"""The torture suite: one boot, then step after step over the dev shell until
a deadline, with the player's health judged between steps. It finds races and
leaks under load that a boot-per-step soak cannot.

    arb yp3 torture build/torture-elf/rockbox.elf build/torture/RUN --hours 6

A step runs one scenario through the shell (`TortureSuite.sh`, `keys`). What
is judged, and when:

    counters  the `health` line's counters that must never rise (`counters`:
              PCM underruns, playback stalls, the UI thread missing its ping),
              from where a scenario takes its baseline to its check_health();
              a scenario names those its own work raises on purpose
    after     every step: the suite's after-step checks (after_step: a Rockbox
              build's messages, decode hashes, the charger)
    busy      a background job the next step must not start under (busy(): a
              database build), waited for up to BUSY_WAIT_S
    free      free memory read in comparable states (settled()): its ceiling
              must not fall more than FREE_DRIFT below the first after a boot
    lost      the shell goes away or stops replying: the player crashed, hung
              or reset. The suite waits for it in the host's flashing mode (a
              hang comes back by itself through the recovery watchdog), keeps
              what it left in crash-N.txt (the commands before, the reply cut
              short, the target's records: log, fault, threads, and the first
              log line that names a crash) and boots it again. A return the
              target expects (a menu power-off) is no failure.

Scenarios register with this module's `scenario` (the engine's, for this
suite) and get the suite itself, a player's subclass of TortureSuite that
knows its shell's commands (`@player_suite("yp3")`). They record problems with
fail(), and return the step's note. Rockbox dev builds share one shell: their
suites subclass RockboxTorture (suites/rockbox_torture.py), which holds the
scenarios every player runs; a player's module (targets/NAME/torture.py)
adds what only its own shell or hardware has.

The dev shell contract (skills/adding-a-target.md): one command per line, the
reply ended by `ok` or `err ...`; `health [reset]` answers a line "health KEY
VALUE ..." with numbers (or "A/B": A counts) for every counter, the free memory
and what busy() and settled() read; `!rom` sends the player back to the host
even while a command blocks the shell.

The run directory gets steps.jsonl and NNN-name.txt (each step's commands and
replies) as every run does, crash-N.txt, and report.md, rewritten after every
step. Each run that got going adds a line to the ledger, the running total of
hardware test time (`arb torture-stats` sums it):

    [torture]
    ledger = "docs/torture-runs.csv"    # run,hours,steps,failures,crashes,seed,scenarios
"""

from __future__ import annotations

import re
import sys
import time
from argparse import Namespace
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from random import Random
from typing import ClassVar, cast

from .. import attention, engine
from ..engine import Outcome, StepContext, Suite
from ..errors import ArbError, UsageError
from ..lock import DeviceLock
from ..registry import Context, arg, command
from ..shell import NoShell, Reply
from ..target import RunRequest, Target
from .soak import FAULTS

BUSY_WAIT_S = 300           # a background job must end within this after a step
FREE_DRIFT = 16 * 1024      # bytes the free memory ceiling may fall after a boot
FREE_WINDOW = 8             # settled readings a ceiling is the highest of
HISTORY = 40                # commands a crash report lists
LOG_TAIL = 120              # dev log lines a crash report keeps
RETURN_WAIT_S = 60          # a crashed or hung player comes back by itself within this
ASK_WAIT_S = 240            # ...one asked to (`!rom`) within this
HOST_WAIT_S = 900           # before a boot, how long the player may take to reach the host
SHELL_WAIT_S = 600          # the dev shell is up within this after a launch
LEDGER_HEAD = "run,hours,steps,failures,crashes,seed,scenarios"


class TortureError(ArbError):
    """The run cannot go on: the player is not back, or not set up"""


class Lost(Exception):
    """The player stopped answering mid-step: the shell went away or did not
    reply (a state, as engine's Outcome.lost). TAIL: the last reply lines
    that did arrive."""

    def __init__(self, why: str, tail: list[str] | None = None) -> None:
        super().__init__(why)
        self.tail = tail or []


# ---------------------------------------------------------------- health

@dataclass(frozen=True)
class Health:
    """One `health` reply line as KEY VALUE pairs"""

    line: str
    fields: dict[str, str]

    def num(self, key: str, default: int = 0) -> int:
        """KEY's number; the first of an "A/B" pair"""
        v = self.fields.get(key)
        return default if v is None else int(v.split("/")[0])

    def pair(self, key: str) -> tuple[int, int]:
        a, _, b = self.fields.get(key, "0/0").partition("/")
        return int(a), int(b or 0)

    def text(self, key: str, default: str = "-") -> str:
        return self.fields.get(key, default)


def parse_health(lines: Iterable[str], words: Iterable[str] = ()) -> Health | None:
    """The first "health ..." line: every KEY NUMBER (or KEY A/B) pair, and
    WORDS, keys whose value is a word"""
    for line in lines:
        if line.startswith("health "):
            fields = dict(re.findall(r"(\w+) (-?\d+(?:/\d+)?)(?!\S)", line))
            for w in words:
                if m := re.search(rf"\b{w} (\S+)", line):
                    fields[w] = m[1]
            return Health(line, fields)
    return None


class FreeDrift:
    """Free memory read in comparable states, per boot. The ceiling of the
    first WINDOW readings is the reference; when the ceiling of the latest
    WINDOW falls more than BUDGET below it, that is a leak, reported once
    and then measured from the new level."""

    def __init__(self, budget: int = FREE_DRIFT, window: int = FREE_WINDOW) -> None:
        self.budget, self.window = budget, window
        self.readings: list[int] = []
        self.reference: int | None = None

    def reset(self) -> None:
        self.readings, self.reference = [], None

    def add(self, free: int) -> str | None:
        self.readings.append(free)
        if len(self.readings) < self.window:
            return None
        if self.reference is None:
            self.reference = max(self.readings[:self.window])
            return None
        now = max(self.readings[-self.window:])
        if now >= self.reference - self.budget:
            return None
        fell, self.reference = self.reference - now, now
        return (f"free memory ceiling {now} B, {fell} B down over "
                f"{len(self.readings)} settled readings since the boot")


# ---------------------------------------------------------------- the ledger

@dataclass(frozen=True)
class LedgerRow:
    run: str                    # the start, YYYYmmdd-HHMMSS
    hours: float
    steps: int
    failures: int
    crashes: int
    seed: int
    scenarios: str              # those that ran, space-separated

    def csv(self) -> str:
        return (f"{self.run},{self.hours:.2f},{self.steps},{self.failures},{self.crashes},"
                f"{self.seed},{self.scenarios}")


def ledger_append(path: Path, row: LedgerRow) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LEDGER_HEAD + "\n")
    with path.open("a") as f:
        f.write(row.csv() + "\n")


def ledger_rows(path: Path) -> list[LedgerRow]:
    rows = []
    for line in path.read_text().splitlines()[1:]:
        if line:
            r = line.split(",", 6)
            rows.append(LedgerRow(r[0], float(r[1]), int(r[2]), int(r[3]), int(r[4]),
                                  int(r[5]), r[6] if len(r) > 6 else ""))
    return rows


def ledger_totals(rows: list[LedgerRow]) -> list[str]:
    if not rows:
        return ["no torture runs in the ledger"]
    days = sorted(r.run[:8] for r in rows)
    long = [r for r in rows if r.hours >= 1]
    return [f"{len(rows)} torture runs on hardware, {sum(r.hours for r in rows):.1f} h, "
            f"{sum(r.steps for r in rows)} scenario steps, {sum(r.crashes for r in rows)} "
            f"crashes/hangs caught ({days[0]}..{days[-1]})",
            f"runs >= 1 h: {len(long)}, {sum(r.hours for r in long):.1f} h"]


# ---------------------------------------------------------------- the suite

Records = list[tuple[str, str]]     # (title, text) of what a crashed player left


class TortureSuite(Suite):
    """The torture run on a player with a dev shell. A player's subclass
    (player_suite) knows its shell's commands; these defaults only use the
    shell contract and the Target API."""

    name = "torture"
    counters: ClassVar[tuple[str, ...]] = ("underruns", "stalls", "ui_late")
    health_words: ClassVar[tuple[str, ...]] = ()    # health keys whose value is a word
    free_key: ClassVar[str] = "free"
    crash_lines: ClassVar[str] = ""     # log lines that name a crash, on top of soak's FAULTS
    line_max: ClassVar[int] = 0         # longest command line the shell takes (0: any)

    def __init__(self, target: Target, image: Path, *, boot: bool = True,
                 after_boot: Iterable[str] = (), ledger: Path | None = None,
                 seed: int = 0) -> None:
        super().__init__(target)
        self.image = image
        self.do_boot = boot
        self.after_boot = list(after_boot)
        self.ledger = ledger
        self.seed = seed
        self.shell = target.shell()
        self.out = Path()
        self.history: deque[str] = deque(maxlen=HISTORY)
        self.tally: dict[str, list[int]] = {}   # scenario: [steps, failed]
        self.crashes = 0
        self.returns = 0                        # expected returns (crash files too)
        self.drift = FreeDrift()
        self.t0 = time.time()
        self.started = ""
        self.running = False
        self._lock: DeviceLock | None = None
        extra = "|".join(x for x in (target.log_faults, self.crash_lines) if x)
        self.crash_re = re.compile(FAULTS + (f"|{extra}" if extra else ""), re.I)
        # the step under way
        self._step: StepContext | None = None
        self.rng = Random()
        self.problems: list[str] = []
        self.notes: list[str] = []
        self.transcript: list[str] = []

    # ---- the run

    def start(self, out: Path) -> None:
        self.out = out
        self._lock = self.target.lock()
        self._lock.__enter__()          # held for the run (released by close())
        self.t0 = time.time()
        self.started = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            if self.do_boot:
                self.boot()
            else:
                self.prepare()
                self.sh("health reset")
            self.preflight()
        except Lost as e:
            raise TortureError(f"the player stopped answering before the first step: {e}") from e
        self.running = True

    def finish(self, out: Path, steps: list[engine.Step]) -> None:
        with suppress(Lost, ArbError):
            self.wrap_up()
        self.write_report()
        print(f"report: {self.out / 'report.md'}")

    def close(self) -> None:
        """After the run, also one cut short: the ledger line, the device lock"""
        if self.running and self.ledger:
            ledger_append(self.ledger, LedgerRow(
                self.started, (time.time() - self.t0) / 3600,
                sum(n for n, _ in self.tally.values()), sum(f for _, f in self.tally.values()),
                self.crashes, self.seed, " ".join(sorted(self.tally))))
            print(f"ledger: {self.ledger}")
        self.release()
        if self._lock:
            self._lock.__exit__(None, None, None)
            self._lock = None

    def run_step(self, ctx: StepContext, body: Callable[[], str | None]) -> Outcome:
        """One scenario: BODY's note, the problems it recorded, its commands
        and replies; a lost player collected"""
        self._step, self.rng = ctx, ctx.rng
        self.problems, self.notes, self.transcript = [], [], []
        try:
            note = body() or ""
        except Lost as e:
            why, expected = self.collect(e)
            if not expected:
                self.count(ctx.name, failed=True)
                return Outcome([*self.problems, why], output=self.output(), lost=True)
            try:
                self.boot()
            except (ArbError, Lost) as b:
                return Outcome([*self.problems, f"{why}; no boot after it: {b}"],
                               output=self.output(), lost=True)
            note = why
        return Outcome(list(self.problems), "; ".join(filter(None, [note, *self.notes])),
                       self.output())

    def check(self, step: StepContext, outcome: Outcome) -> list[str]:
        problems: list[str] = []
        try:
            problems += self.after_step()
            problems += self.settle()
        except Lost as e:
            why, expected = self.collect(e)
            if not expected:
                problems.append(f"after the step: {why}")
            try:
                self.boot()
            except (ArbError, Lost) as b:
                problems.append(f"no boot after it: {b}")
        self.count(step.name, failed=bool(outcome.problems or problems))
        return problems

    def recover(self, step: StepContext, outcome: Outcome) -> bool:
        """The crash was collected in the step; boot again"""
        try:
            self.boot()
        except (ArbError, Lost) as e:
            print(f"torture: no way back: {e}", file=sys.stderr)
            with suppress(OSError):
                attention.ring(attention.settings(self.target.ws)[0],
                               f"torture: the {self.target.name} needs a hand: {e}")
            return False
        return True

    def count(self, scenario: str, failed: bool) -> None:
        st = self.tally.setdefault(scenario, [0, 0])
        st[0] += 1
        st[1] += failed
        self.write_report()

    @property
    def step(self) -> StepContext:
        if self._step is None:
            raise RuntimeError("no step under way")
        return self._step

    def step_file(self, what: str, suffix: str) -> Path:
        """A further file of the step under way: NNN-name-WHAT[-k]SUFFIX"""
        path, k = self.step.path(f"-{what}{suffix}"), 2
        while path.exists():
            path, k = self.step.path(f"-{what}-{k}{suffix}"), k + 1
        return path

    def fail(self, problem: str) -> None:
        self.problems.append(problem)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def output(self) -> str:
        return "\n".join(self.transcript) + "\n"

    # ---- the shell

    def shell_up(self) -> bool:
        try:
            self.shell.port()
        except NoShell:
            return False
        return True

    def reply(self, line: str, timeout: float = 120.0) -> Reply:
        """LINE's whole reply; Lost if the shell went away or did not answer"""
        stamp = time.strftime("%H:%M:%S")
        self.history.append(f"{stamp} {line}")
        self.transcript.append(f"{stamp} > {line}")
        try:
            r = self.shell(line, timeout)
        except NoShell:
            raise Lost(f"shell gone before: {line}") from None
        except OSError as e:
            raise Lost(f"shell gone during: {line} ({e})") from None
        self.transcript += r.lines
        if r.timed_out:
            time.sleep(3)
            raise Lost(f"{'no reply to' if self.shell_up() else 'shell gone during'}: {line}",
                       r.lines[-6:])
        return r

    def sh(self, line: str, timeout: float = 120.0) -> list[str]:
        """LINE's reply lines but the closing `ok`"""
        return [x for x in self.reply(line, timeout).lines if x != "ok"]

    def keys(self, script: str) -> None:
        """A key script, in as many `keys` commands as the shell's line takes"""
        room = self.line_max - len("keys ") if self.line_max else 0
        part: list[str] = []
        for tok in script.split():
            if room and part and len(" ".join([*part, tok])) > room:
                self.sh("keys " + " ".join(part), timeout=300)
                part = []
            part.append(tok)
        if part:
            self.sh("keys " + " ".join(part), timeout=300)

    def parse_health(self, lines: Iterable[str]) -> Health | None:
        return parse_health(lines, self.health_words)

    def health(self) -> Health:
        for _ in range(3):
            lines = self.sh("health")
            if h := self.parse_health(lines):
                return h
            # once seen from a player that still answered `rom` afterwards
            self.history.append(f"  no health line in: {' | '.join(lines)[:300]}")
            time.sleep(5)
        raise Lost("no health line in three replies")

    def check_health(self, before: Health, ignore: Iterable[str] = ()) -> Health:
        """Fails the step on any counter that rose since BEFORE (but IGNORE);
        the reading after, a baseline for the next check"""
        after = self.health()
        skip = set(ignore)
        rises = [f"{k} +{after.num(k) - before.num(k)}" for k in self.counters
                 if k not in skip and after.num(k) > before.num(k)]
        if rises:
            self.fail(", ".join(rises))
            self.sh("log")              # what led to it, in the step's output
        return after

    def settle(self) -> list[str]:
        """A background job waited for; then the free memory, if settled"""
        t0 = time.monotonic()
        h = self.health()
        while what := self.busy(h):
            if time.monotonic() - t0 > BUSY_WAIT_S:
                return [f"{what} still running after {BUSY_WAIT_S} s"]
            time.sleep(5)
            h = self.health()
        leak = self.drift.add(h.num(self.free_key)) if self.settled(h) else None
        return [leak] if leak else []

    # ---- boots and crashes

    def to_host(self) -> None:
        """The player in the host's flashing mode: asked through its shell if
        that is up, else waited for"""
        if self.shell_up():
            if not self.back_to_host(ASK_WAIT_S):
                raise TortureError(f"the {self.target.name} did not go back to the host")
        elif not self.wait_host(HOST_WAIT_S):
            raise TortureError(f"the {self.target.name} did not reach the host in "
                               f"{HOST_WAIT_S} s: it needs its manual recovery")

    def boot(self) -> None:
        """The image afresh: the player back on the host, launched with its
        shell, set up (prepare) and its counters zeroed"""
        self.to_host()
        req = self.boot_request()
        self.target.before_run(req)
        self.target.launch(req)
        end = time.monotonic() + SHELL_WAIT_S
        while not self.shell_up():
            if time.monotonic() > end:
                raise TortureError(f"no dev shell {SHELL_WAIT_S} s after the boot")
            time.sleep(3)
        time.sleep(5)
        self.prepare()
        self.sh("health reset")
        self.drift.reset()

    def collect(self, e: Lost) -> tuple[str, bool]:
        """After a lost reply: the player back on the host and what it left
        in crash-N.txt; the step's problem (or note), and whether the target
        expected that return"""
        home = self.wait_host(RETURN_WAIT_S) or self.back_to_host(ASK_WAIT_S)
        if home:
            try:
                records = self.records()
            except (ArbError, OSError, ValueError) as err:     # the crash file still gets written
                records = [("records", f"unreadable: {err}")]
        else:
            records = [("records", "none: the player did not come back to the host")]
        expected = self.expected_return(records) if home else None
        n = self.crashes + self.returns + 1
        path = self.out / f"crash-{n}.txt"
        kind = self.crash_kind(records)
        text = [f"{expected}: {e}" if expected else f"{e}", "", f"== crash: {kind}", "",
                "== commands", *self.history]
        if e.tail:
            text += ["", "== reply received (last lines)", *map(repr, e.tail)]
        for title, body in records:
            text += ["", f"== {title}", body.rstrip("\n")]
        path.write_text("\n".join(text) + "\n")
        if expected:
            self.returns += 1
            return f"{expected} ({e}; {path.name})", True
        self.crashes += 1
        return f"{e}: {kind} ({path.name})", False

    def crash_kind(self, records: Records) -> str:
        """The first log line that names a crash"""
        for title, body in records:
            if "log" in title:
                hit = next((ln.strip() for ln in body.splitlines()
                            if self.crash_re.search(ln)), None)
                if hit:
                    return hit[:160]
        return "no crash line in the log (a hang, a reset, or the shell itself)"

    # ---- what a player's subclass provides

    def boot_request(self) -> RunRequest:
        """The launch: the image with USB (and so the shell), never returning"""
        return RunRequest(self.image, 0, usb=True)

    def prepare(self) -> None:
        """After each boot (and at the start of a run on a running player)"""
        for line in self.after_boot:
            self.sh(line)

    def preflight(self) -> None:
        """Once before the first step: raise TortureError if the player is
        not fit for the run (e.g. its card's modules are from another build)"""

    def after_step(self) -> list[str]:
        """Problems every step is checked for"""
        return []

    def busy(self, h: Health) -> str | None:
        """What background job H shows running, if one the next step must wait for"""
        return None

    def settled(self, h: Health) -> bool:
        """Whether H's free memory is comparable with other such readings"""
        return False

    def wait_host(self, timeout: float) -> bool:
        """Whether the player is (or comes) back in the host's flashing mode"""
        return self.target.wait_return(time.monotonic() + timeout, lambda: None)

    def back_to_host(self, timeout: float) -> bool:
        """Asks a running player back to the host"""
        with suppress(Lost):
            self.reply("!rom", 10)
        return self.wait_host(timeout)

    def records(self) -> Records:
        """What a player back on the host left (the caller holds the lock)"""
        log = self.target.read_log(self.image)
        return [("dev log (tail)", "\n".join(log.splitlines()[-LOG_TAIL:]))]

    def expected_return(self, records: Records) -> str | None:
        """Why the return was expected (no failure), if it was"""
        return None

    def report_lines(self) -> list[str]:
        """The player's own totals for report.md"""
        return []

    def wrap_up(self) -> None:
        """After the last step: leave the player as a person would find it"""

    def release(self) -> None:
        """Stop what the run started on this PC"""

    # ---- report

    def write_report(self) -> None:
        hours = (time.time() - self.t0) / 3600
        lines = [f"# Torture run {self.started}", "",
                 f"{self.target.name}: {self.image}, seed {self.seed}", "",
                 f"Duration: {hours:.2f} h, crashes/hangs: {self.crashes}, "
                 f"expected returns: {self.returns}", "",
                 "| scenario | steps | failed |", "|---|---|---|"]
        lines += [f"| {k} | {n} | {f} |" for k, (n, f) in sorted(self.tally.items())]
        lines += self.report_lines()
        lines += ["", "Failures: steps.jsonl and NNN-name.txt; crashes: crash-N.txt"]
        (self.out / "report.md").write_text("\n".join(lines) + "\n")


_players: dict[str, type[TortureSuite]] = {}


def player_suite[S: type[TortureSuite]](*targets: str) -> Callable[[S], S]:
    """Registers a TortureSuite subclass as the torture run of TARGETS"""
    def register(cls: S) -> S:
        for t in targets:
            _players[t] = cls
        return cls
    return register


def suite_class(target: str) -> type[TortureSuite]:
    return _players.get(target, TortureSuite)


class Register:
    """What `scenario` returns: registers a function of the suite as a step"""

    def __init__(self, name: str, weight: int, target: Iterable[str] | None,
                 needs: Iterable[str]) -> None:
        self.name, self.weight, self.target, self.needs = name, weight, target, needs

    def __call__[S: TortureSuite](self, fn: Callable[[S], str | None]) -> Callable[[S], str | None]:
        def step(ctx: StepContext) -> Outcome:
            suite = cast(S, ctx.suite)
            return suite.run_step(ctx, lambda: fn(suite))
        engine.scenario(self.name, suite=TortureSuite.name, weight=self.weight,
                        target=self.target, needs=self.needs)(step)
        return fn


def scenario(name: str, *, weight: int = 1, target: Iterable[str] | None = None,
             needs: Iterable[str] = ()) -> Register:
    """engine.scenario for torture steps: FN gets the suite (the player's
    subclass), records problems with fail() and returns the step's note"""
    return Register(name, weight, target, needs)


# ---------------------------------------------------------------- commands

@command("torture", help="one boot, then random scenarios over the dev shell until a deadline",
         needs={"shell"}, args=[
             arg("image", type=Path, help="the dev build's rockbox.elf, in a directory of its "
                 "own: every recovery boots that file again"),
             arg("out", type=Path, help="run directory: steps.jsonl, NNN-name.txt, crash-N.txt, "
                 "report.md"),
             arg("--hours", type=float), arg("--until", metavar="HH:MM"),
             arg("--only", help="comma-separated scenarios"),
             arg("--weights", default="", help="name=weight,... overriding the defaults "
                 "(0 leaves one out, a positive weight takes in one that is off by default)"),
             arg("--seed", type=int),
             arg("--no-boot", action="store_true",
                 help="the image already runs with its shell: start on it"),
             arg("--after-boot", action="append", default=[], metavar="COMMAND",
                 help='a shell command after every boot, e.g. "cpu max", "tickprio 0" '
                      '(repeatable)'),
             arg("--ledger", type=Path, help="CSV the run's line goes to (default: [torture] "
                 "ledger; none: no line)"),
         ])
def torture_cmd(ctx: Context, args: Namespace) -> int:
    """Boots IMAGE with USB on (unless --no-boot) and runs weighted random
    scenarios through its dev shell until --hours or --until. A player that
    stops answering is collected (crash-N.txt), booted again, and the run
    goes on. Exit status 1 if any step failed."""
    if not args.image.is_file():
        raise UsageError(f"{args.image}: no such image")
    seed = args.seed if args.seed is not None else int(datetime.now().timestamp())
    weights = {k: int(v) for k, _, v in (w.partition("=") for w in args.weights.split(",") if w)}
    ledger = args.ledger or ctx.ws.section("torture").opt_path("ledger")
    s = suite_class(ctx.t.name)(ctx.t, args.image, boot=not args.no_boot,
                                after_boot=args.after_boot, ledger=ledger, seed=seed)
    try:
        steps = engine.run(s, engine.RunSettings(
            args.out, engine.deadline(args.hours, args.until), seed,
            args.only.split(",") if args.only else None, weights),
            echo=lambda line: print(line, flush=True))
    finally:
        s.close()
    return 0 if steps and all(x.ok for x in steps) else 1


@command("torture-stats", help="the torture ledger's totals: runs, hours, steps, crashes",
         target=False, args=[arg("ledger", type=Path, nargs="?",
                                 help="default: [torture] ledger")])
def torture_stats(ctx: Context, args: Namespace) -> int:
    path = args.ledger or ctx.ws.section("torture").opt_path("ledger")
    if path is None:
        raise UsageError("no LEDGER given and no [torture] ledger")
    print("\n".join(ledger_totals(ledger_rows(path))))
    return 0
