"""The workspace: a port's checkout and its `auto-rockbox.toml`.

Everything that belongs to one person's bench rather than to a player lives
here: where builds and captures are, the toolchain, the card's UUID, the
camera's rotation, Bluetooth addresses. The file is found by walking up from
the current directory, like pyproject.toml.

    [paths]
    build = "build"
    toolchain = "toolchain/gcc-arm-none-eabi-10.3-2021.10/bin/arm-none-eabi-"

    [webcam]
    latest = "analysis/webcam/latest.png"
    rotate = 180

    [targets.ts17]
    card_uuid = "0C47-FBCA"

    [plugins]
    modules = ["tools/arb_local.py"]
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAME = "auto-rockbox.toml"


class ConfigError(Exception):
    """A missing or malformed workspace setting"""


@dataclass
class Section:
    """One table of the config, with typed getters that name the key on error"""

    name: str
    data: dict[str, Any]
    root: Path

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def string(self, key: str, default: str | None = None) -> str:
        v = self.data.get(key, default)
        if not isinstance(v, str):
            raise ConfigError(f"[{self.name}] {key}: a string is needed")
        return v

    def integer(self, key: str, default: int | None = None) -> int:
        v = self.data.get(key, default)
        if not isinstance(v, int):
            raise ConfigError(f"[{self.name}] {key}: an integer is needed")
        return v

    def number(self, key: str, default: float | None = None) -> float:
        v = self.data.get(key, default)
        if not isinstance(v, int | float):
            raise ConfigError(f"[{self.name}] {key}: a number is needed")
        return float(v)

    def path(self, key: str, default: str | None = None) -> Path:
        """A path, relative to the workspace root unless absolute"""
        return self.root / Path(self.string(key, default)).expanduser()

    def opt_path(self, key: str) -> Path | None:
        return self.path(key) if key in self.data else None


@dataclass
class Workspace:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def find(cls, start: Path | None = None) -> Workspace:
        """The nearest auto-rockbox.toml at or above START; without one, START
        itself with every setting at its default"""
        here = (start or Path.cwd()).resolve()
        for d in (here, *here.parents):
            f = d / CONFIG_NAME
            if f.is_file():
                with f.open("rb") as fh:
                    return cls(d, tomllib.load(fh))
        return cls(here)

    def section(self, *names: str) -> Section:
        d: Any = self.data
        for n in names:
            d = d.get(n, {}) if isinstance(d, dict) else {}
        return Section(".".join(names), d if isinstance(d, dict) else {}, self.root)

    def target(self, name: str) -> Section:
        return self.section("targets", name)

    @property
    def paths(self) -> Section:
        return self.section("paths")

    @property
    def build_dir(self) -> Path:
        return self.paths.path("build", "build")

    def tool(self, name: str) -> str:
        """A binutils tool of the cross toolchain (paths.toolchain is its prefix)"""
        prefix = self.paths.get("toolchain")
        if prefix is None:
            return f"arm-none-eabi-{name}"
        return str(self.root / f"{prefix}{name}")
