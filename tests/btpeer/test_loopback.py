"""The peers end to end without the simulator: a Bumble host plays the stack
under test on controller A (over the peer's H4 server), pages the peer,
pairs and encrypts. The sink must pass three seconds of a 1 kHz tone; the
DualShock 4's presses must reach the stack's HID host. Needs Bumble (and
ffmpeg for the tone); takes about 7 s."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import shutil
import socket
import tempfile
from collections.abc import Coroutine, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from auto_rockbox.btpeer.options import HidOptions, SinkOptions
from tests import Skip

from .fixtures import make_tone


def _need_bumble() -> None:
    if importlib.util.find_spec("bumble") is None:
        raise Skip("Bumble is not installed (uv sync --extra bt)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _logged(path: Path) -> Generator[None]:
    """Every log line into PATH, also Bumble's and asyncio's warnings as the
    stack's side is torn down"""
    handler = logging.FileHandler(path)
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
        handler.close()


def _against(peer: Coroutine[Any, Any, int], stack: Coroutine[Any, Any, None]) -> int:
    """The peer's exit status, with the stack started once the peer is up"""
    async def both() -> int:
        run = asyncio.ensure_future(peer)
        await asyncio.sleep(0.2)
        other = asyncio.ensure_future(stack)
        try:
            return await asyncio.wait_for(run, 30)
        finally:
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)
    return asyncio.run(both())


def test_sink_passes_a_clean_stream() -> None:
    _need_bumble()
    if shutil.which("ffmpeg") is None:
        raise Skip("no ffmpeg")
    from auto_rockbox.btpeer import sink  # noqa: PLC0415

    from .stack import stream_to  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        make_tone(out / "tone.sbc", 3.0)
        port = _free_port()
        opts = SinkOptions(out=out, hci=f"127.0.0.1:{port}", seconds=2.0)
        with _logged(out / "peer.log"):
            rc = _against(sink.run(opts), stream_to(port, opts.sink_address, out / "tone.sbc"))
        text = (out / "peer.log").read_text()
        assert rc == 0, text[-3000:]
        assert "AVDTP SET_CONFIGURATION accepted" in text, text[-3000:]
        assert "RESULT: PASS" in text
        assert (out / "keys.json").exists()            # the sink bonded


def test_ds4_presses_reach_the_stack() -> None:
    _need_bumble()
    from auto_rockbox.btpeer import hid  # noqa: PLC0415

    from .stack import hid_host  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        port = _free_port()
        opts = HidOptions(out=out, hci=f"127.0.0.1:{port}", press=["up", "a"], step=0.3,
                          hold=0.1, rate=20.0)
        reports: list[bytes] = []
        with _logged(out / "peer.log"):
            rc = _against(hid.run_sim(opts), hid_host(port, opts.device_address, reports))
        text = (out / "peer.log").read_text()
    assert rc == 0, text[-3000:]
    assert "HID up" in text and "STACK VIOLATION" not in text, text[-3000:]
    # DATA (0xA1), input report 0x01: LX LY RX RY, then the hat and buttons
    buttons = [r[6] for r in reports if r[:2] == b"\xa1\x01"]
    assert len(buttons) >= 10                       # it streams, pressed or not
    assert any(b & 0x0F == 0 for b in buttons)      # the hat up
    assert any(b & 0x20 for b in buttons)           # a (cross)
    assert buttons[-1] == 0x08                      # all released, hat centred
