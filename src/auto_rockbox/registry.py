"""Commands and targets, and how plugins add to them.

A target (a player) is a `Target` subclass registered with `@register_target`.
A command is a function registered with `@command`:

    @command("log", help="print the dev log")             # every target
    def log(ctx: Context, args: Namespace) -> int: ...

    @command("norprobe", help="...", target={"ts17"},     # one player only
             args=[arg("what"), arg("--at", type=lambda s: int(s, 0))])
    def norprobe(ctx: Context, args: Namespace) -> int: ...

    @command("media", help="...", target=False)           # host only: `arb media`

A player's command overrides a generic one of the same name for that player.
`needs` names target capabilities (`Target.capabilities`); a command is only
offered to targets that have them all.

Where registrations come from, in order: the built-in modules
(auto_rockbox.commands, auto_rockbox.targets), the `auto_rockbox.plugins`
entry points of installed packages, and the workspace's `[plugins] modules`
(Python files in the port's checkout). Each is simply imported; its
decorators do the registering.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import sys
from argparse import Namespace
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .target import Target
    from .workspace import Workspace

PLUGIN_GROUP = "auto_rockbox.plugins"


@dataclass
class Context:
    """What a command gets: the workspace and, for target commands, the target"""

    ws: Workspace
    target: Target | None = None

    @property
    def t(self) -> Target:
        if self.target is None:
            raise RuntimeError("this command needs a target")
        return self.target


CommandFn = Callable[[Context, Namespace], int | None]


@dataclass(frozen=True)
class Arg:
    flags: tuple[str, ...]
    kwargs: dict[str, Any]


def arg(*flags: str, **kwargs: Any) -> Arg:
    """An argparse argument: arg("--keys", help=...) as add_argument would take it"""
    return Arg(flags, kwargs)


@dataclass
class Command:
    name: str
    fn: CommandFn
    help: str
    args: list[Arg] = field(default_factory=list)
    targets: frozenset[str] | None = None   # None: every target
    host: bool = False                      # no target at all
    needs: frozenset[str] = frozenset()
    description: str = ""

    def offered_to(self, target: type[Target]) -> bool:
        if self.host:
            return False
        if self.targets is not None and target.name not in self.targets:
            return False
        return self.needs <= target.capabilities


_commands: list[Command] = []
_targets: dict[str, type[Target]] = {}


def command(name: str | None = None, *, help: str, args: Iterable[Arg] = (),
            target: bool | Iterable[str] = True, needs: Iterable[str] = ()
            ) -> Callable[[CommandFn], CommandFn]:
    def register(fn: CommandFn) -> CommandFn:
        host = target is False
        targets = None if isinstance(target, bool) else frozenset(target)
        default = getattr(fn, "__name__", "").removesuffix("_cmd").replace("_", "-")
        _commands.append(Command(name or default, fn, help, list(args),
                                 targets, host, frozenset(needs), (fn.__doc__ or "").strip()))
        return fn
    return register


def register_target[T: type[Target]](cls: T) -> T:
    if cls.name in _targets and _targets[cls.name] is not cls:
        raise ValueError(f"target {cls.name!r} registered twice")
    _targets[cls.name] = cls
    return cls


def targets() -> dict[str, type[Target]]:
    return dict(sorted(_targets.items()))


def host_commands() -> dict[str, Command]:
    return {c.name: c for c in _commands if c.host}


def commands_for(target: type[Target]) -> dict[str, Command]:
    """The commands TARGET offers; its own beat generic ones of the same name"""
    out: dict[str, Command] = {}
    for c in _commands:
        if c.offered_to(target) and (c.name not in out or c.targets is not None):
            out[c.name] = c
    return dict(sorted(out.items()))


def _import_file(path: Path) -> None:
    name = "auto_rockbox_local_" + path.stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


def load_plugins(ws: Workspace) -> None:
    """Import every source of commands and targets (see the module docstring)"""
    importlib.import_module("auto_rockbox.commands")
    importlib.import_module("auto_rockbox.targets")
    for ep in importlib.metadata.entry_points(group=PLUGIN_GROUP):
        ep.load()
    for m in ws.section("plugins").get("modules", []):
        _import_file(ws.root / m)
