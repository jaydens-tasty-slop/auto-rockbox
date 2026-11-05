"""bt-peer pcheadset: this PC's adapter as a headset that pages the player
again and again, the way a Sony WH-1000XM3 does at each power-on, with one
line per cycle saying how far the link got.

Bumble drives the adapter over raw USB (it needs access to the device; BlueZ
loses the adapter while this runs). Each cycle the headset pages the player,
authenticates (Just Works pairing the first time) and encrypts; switch_after
s after the page completes it asks for the peripheral role, as the XM3 does
(its link manager pauses encryption for the switch; a range A:B picks a time
in it each cycle, switch_share the share of cycles that switch). It queries
the player's SDP records and leaves AVDTP to the player, as the XM3 does. The
cycle passes once the stream started and `play` s of media arrived; then the
headset disconnects (0x13, as at power-off) and waits `gap` s. A stage not
reached within `deadline` s of the page fails the cycle and the link is
disconnected. After a link lost with 0x08 or 0x22 the player should page the
headset itself (within repage_wait s); the next cycle then takes that link
("paged by the player", as it does when its page finds the link already up)
and passes once the music is back; without that page it fails ("no repage")
and pages the player itself.

Range loss: in lose_share of the cycles the headset vanishes once the music
plays (its adapter is reset: no LMP_detach, so the player sees a supervision
timeout, 0x08, as out of range) and the next cycle waits for the player's
page. relose_share of those pages vanish again as soon as they are up,
before the stream (still at the edge): the player must keep paging.

On the player: Bluetooth on and music playing to the Bluetooth sink. It
answers the pages and resumes the music for the headset by itself.

Outputs in OUT: cycles.txt (one line per cycle: result, then stage:seconds,
then notes), headset.log, hci.btsnoop, keys.json. The exit status does not
say whether the cycles passed; cycles.txt does.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from bumble import avdtp, avrcp, core, hci, sdp
from bumble.core import PhysicalTransport
from bumble.device import Connection
from bumble.host import Host
from bumble.snoop import BtSnooper
from bumble.transport import open_transport

from .logs import log, peer_logging, why
from .options import HeadsetOptions
from .profiles import (
    HEADPHONES_COD,
    authenticate,
    classic_device,
    just_works,
    sink_capabilities,
    sink_records,
)

E = hci.HCI_ErrorCode
PAGE_S = 15.0
DISCONNECT_S = 5.0
GONE_S = 60.0                   # for the link to go after the headset disconnects
THIN_PACKETS_PER_S = 10         # fewer media packets than this over `play`: thin media
ALLOW_ROLE_SWITCH = 0x0001      # default link policy (the adapter's default has none)
LOST = (E.CONNECTION_TIMEOUT_ERROR, E.LMP_OR_LL_RESPONSE_TIMEOUT_ERROR)


class Cycle:
    def __init__(self, n: int) -> None:
        self.n = n
        self.t0 = time.monotonic()
        self.stages: list[str] = []
        self.notes: list[str] = []
        self.packets = 0
        self.started = asyncio.Event()
        self.playing = asyncio.Event()     # min_packets arrived

    def mark(self, stage: str) -> None:
        t = time.monotonic() - self.t0
        self.stages.append(f"{stage}:{t:.1f}")
        log.info("cycle %d: %s at %.1f s", self.n, stage, t)

    def note(self, text: str) -> None:
        self.notes.append(text)
        log.info("cycle %d: %s", self.n, text)


class _Done(Exception):
    """The cycle ended as planned before its last stage"""


class Headset:
    """The headset on the adapter; start() resets it, so the links up
    before vanish without a word (that is how the headset leaves range)"""

    def __init__(self, opts: HeadsetOptions, transport: Any, snooper: BtSnooper,
                 rng: random.Random, repage_due: bool = False) -> None:
        self.opts = opts
        self.rng = rng
        self.cycle: Cycle | None = None
        self.paging = False
        self.incoming: Connection | None = None     # the player's page, not yet in a cycle
        self.repage_due = repage_due
        host = Host(transport.source, transport.sink)
        host.snooper = snooper
        self.device = classic_device(opts.name, HEADPHONES_COD, host, opts.out / "keys.json",
                                     just_works())
        self.device.sdp_service_records = sink_records(volume_target=True)
        self.listener = avdtp.Listener.for_device(self.device, (1, 3))
        self.listener.on("connection", self.on_avdtp)
        self.avrcp = avrcp.Protocol(avrcp.Delegate())
        self.avrcp.listen(self.device)
        self.device.on("connection", self.on_connection)

    async def start(self) -> None:
        await self.device.power_on()
        await self.device.set_discoverable(False)
        await self.device.set_connectable(True)
        await self.device.send_command(hci.HCI_Write_Default_Link_Policy_Settings_Command(
            default_link_policy_settings=ALLOW_ROLE_SWITCH))
        log.info("adapter %s up", self.device.public_address)

    @property
    def player(self) -> hci.Address:
        return hci.Address(self.opts.player, hci.Address.PUBLIC_DEVICE_ADDRESS)

    def on_connection(self, conn: Connection) -> None:
        if not self.paging:
            log.info("the player paged us")
            self.incoming = conn

    def player_link(self) -> Connection | None:
        """A link to the player that this headset did not page: the
        player's page came first (our own page then fails with 0x0b)"""
        return self.device.find_connection_by_bd_addr(self.player,
                                                      transport=PhysicalTransport.BR_EDR)

    def on_avdtp(self, protocol: avdtp.Protocol) -> None:
        c = self.cycle
        if c:
            c.mark("avdtp")
        sink = protocol.add_sink(sink_capabilities())
        sink.on("configuration", lambda: c and c.mark("configured"))
        sink.on("start", lambda: self.on_start(c))
        sink.on("rtp_packet", lambda _p: self.on_media(c))

    def on_start(self, c: Cycle | None) -> None:
        if c and not c.started.is_set():
            c.mark("started")
            c.started.set()

    def on_media(self, c: Cycle | None) -> None:
        if c is None:
            return
        c.packets += 1
        if c.packets == self.opts.min_packets:
            c.mark("media")
            c.playing.set()

    async def query_sdp(self, conn: Connection, c: Cycle) -> None:
        try:
            async with sdp.Client(conn) as client:
                for uuid in (core.BT_AUDIO_SOURCE_SERVICE,
                             core.BT_AV_REMOTE_CONTROL_TARGET_SERVICE):
                    await client.search_attributes([uuid], [(0x0000, 0xFFFF)])
            c.mark("sdp")
        except Exception as error:
            c.note(f"sdp failed: {why(error)}")

    async def take_incoming(self, wait: float) -> Connection | None:
        """The player's page (already up, or within WAIT s), or None"""
        end = time.monotonic() + wait
        while self.incoming is None and time.monotonic() < end:
            await asyncio.sleep(0.02)
        conn, self.incoming = self.incoming, None
        if conn is not None and conn.handle not in self.device.connections:
            conn = None             # it went down again
        return conn

    def lose(self, c: Cycle) -> bool:
        """Whether this cycle's link vanishes (lose_share; never the last,
        whose link the player would page again afterwards)"""
        if c.n >= self.opts.cycles or self.rng.random() >= self.opts.lose_share:
            return False
        c.mark("vanished")
        return True

    async def run_cycle(self, n: int, gap: float) -> tuple[bool, bool]:
        """(passed, vanished: the adapter must be reset now)"""
        opts = self.opts
        c = self.cycle = Cycle(n)   # from here on the stream events count
        await asyncio.sleep(gap)
        repage_due = self.repage_due
        # still at the edge: the player's page vanishes again before its
        # stream (it sets up AVDTP within ~0.3 s of the page)
        relose = repage_due and c.n < opts.cycles and self.rng.random() < opts.relose_share
        conn = await self.take_incoming(opts.repage_wait if repage_due else 0)
        c.t0 = time.monotonic()
        deadline = c.t0 + opts.deadline

        def left() -> float:
            return max(0.1, deadline - time.monotonic())
        stage = "page"
        result = "ok"
        if repage_due and conn is None:
            result = f"no repage within {opts.repage_wait:g} s"
        gone = asyncio.Event()
        lost: list[int] = []
        paged_by_player = conn is not None
        vanish = False
        try:
            if conn is None:
                self.paging = True
                try:
                    conn = await asyncio.wait_for(self.device.connect(
                        self.player, transport=PhysicalTransport.BR_EDR), PAGE_S)
                except hci.HCI_Error as error:
                    conn = self.player_link()
                    if error.error_code != E.CONNECTION_ALREADY_EXISTS_ERROR or not conn:
                        raise
                    paged_by_player = True
                finally:
                    self.paging = False
            c.mark("paged by the player" if paged_by_player else "connected")

            def on_disconnection(reason: int) -> None:
                c.note(f"disconnected {reason:#04x}")
                lost.append(reason)
                gone.set()
            conn.on("disconnection", on_disconnection)
            conn.on("role_change", lambda role: c.note(f"role {role.name}"))
            conn.on("role_change_failure", lambda e: c.note(f"role switch failed {e:#04x}"))
            if paged_by_player and relose:
                await asyncio.sleep(self.rng.uniform(0, 0.1))
                if c.started.is_set():
                    c.note("the stream started first")
                c.mark("vanished before the stream")
                vanish = True
                raise _Done
            if paged_by_player:
                # it secures the link and streams by itself
                stage = "stream"
                await asyncio.wait_for(c.started.wait(), left())
                stage = "media"
                await asyncio.wait_for(c.playing.wait(), left())
                await asyncio.sleep(opts.play)
                vanish = self.lose(c)
                raise _Done
            t_conn = time.monotonic()
            stage = "auth"
            await asyncio.wait_for(authenticate(self.device, conn), left())
            c.mark("auth")
            stage = "encrypt"
            await asyncio.wait_for(conn.encrypt(), left())
            c.mark("encrypted")
            sdp_task = asyncio.ensure_future(self.query_sdp(conn, c))
            if self.rng.random() < opts.switch_share:
                after = self.rng.uniform(*opts.switch_after)
                c.note(f"switch at {after:.1f} s")
                await asyncio.sleep(max(0, t_conn + after - time.monotonic()))
                stage = "switch"
                try:
                    await asyncio.wait_for(conn.switch_role(hci.Role.PERIPHERAL), left())
                    c.mark("switched")
                except hci.HCI_Error as error:
                    c.note(f"switch refused {error.error_code:#04x}")
            stage = "stream"
            await asyncio.wait_for(c.started.wait(), left())
            stage = "media"
            await asyncio.wait_for(c.playing.wait(), left())
            before = c.packets
            await asyncio.sleep(opts.play)
            if c.packets - before < opts.play * THIN_PACKETS_PER_S and result == "ok":
                result = f"thin media ({c.packets - before} packets in {opts.play:g} s)"
            sdp_task.cancel()
            vanish = self.lose(c)
        except _Done:
            pass
        except TimeoutError:
            result = f"stall at {stage}"
        except hci.HCI_Error as error:
            result = f"{stage} failed {error.error_code:#04x}"
        except (Exception, asyncio.CancelledError) as error:
            # Bumble cancels what waits on a link that went down
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            result = f"{stage} failed: {why(error)}"
        if conn is not None and not gone.is_set() and not vanish:
            try:
                await asyncio.wait_for(
                    conn.disconnect(E.REMOTE_USER_TERMINATED_CONNECTION_ERROR), DISCONNECT_S)
            except Exception as error:
                c.note(f"disconnect: {why(error)}")
            try:
                await asyncio.wait_for(gone.wait(), GONE_S)
            except TimeoutError:
                c.note(f"link still up {GONE_S:g} s after disconnect")
        # a link lost this way is paged again by the player
        self.repage_due = vanish or any(r in LOST for r in lost)
        line = (f"{time.strftime('%F %T')} cycle {n}: {result}; {' '.join(c.stages)}"
                + (f"; {'; '.join(c.notes)}" if c.notes else ""))
        log.info("%s", line)
        with (opts.out / "cycles.txt").open("a") as f:
            f.write(line + "\n")
        return result == "ok", vanish


async def run(opts: HeadsetOptions) -> None:
    """The cycles; their verdicts are the lines in cycles.txt"""
    rng = random.Random(opts.seed)
    ok = 0
    async with await open_transport(opts.adapter) as transport:
        with (opts.out / "hci.btsnoop").open("wb") as snoop:
            snooper = BtSnooper(snoop)      # one file header for all headsets
            hs = Headset(opts, transport, snooper, rng)
            await hs.start()
            for n in range(1, opts.cycles + 1):
                passed, vanish = await hs.run_cycle(n, opts.gap if n > 1 else 0)
                ok += passed
                log.info("%d of %d ok", ok, n)
                if vanish:
                    hs = Headset(opts, transport, snooper, rng, repage_due=True)
                    await hs.start()


def main(opts: HeadsetOptions, *, limit: float | None = None, echo: bool = True) -> None:
    """The cycles; cycles.txt has a line for each one that ended. TimeoutError
    after LIMIT s. ECHO: the log on stdout too."""
    opts.out.mkdir(parents=True, exist_ok=True)
    with peer_logging(opts.out / "headset.log", echo=echo):
        asyncio.run(asyncio.wait_for(run(opts), limit))
