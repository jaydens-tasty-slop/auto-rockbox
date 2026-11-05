"""What a player provides. A port subclasses `Target`, fills in the class
attributes and the transport methods, and registers it:

    @register_target
    class MyPlayer(Target):
        name = "myplayer"
        summary = "SoC, flash, how it is reached"
        lcd = Lcd(128, 160)
        key_letters = "UDLRS"
        capabilities = frozenset({"framebuffer"})

        def launch(self, req: RunRequest) -> None: ...
        def wait_return(self, deadline: float, tick: Callable[[], None]) -> bool: ...
        def read_log(self) -> str: ...

The generic `run` (auto_rockbox.run) wraps these with the device lock, key
script checks, webcam frames and the framebuffer grab, so a port only writes
its transport.

Capabilities name optional features; commands that need one are only offered
to targets that declare it:
    framebuffer  read_framebuffer() works after a return
    shell        shell() gives a live dev shell (auto_rockbox.shell)
    battery      battery() reads the cell and guards the charger
    cold         RunRequest.cold boots through the normal boot chain
    usb          RunRequest.usb lets Rockbox start its USB stack
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .keys import KeyScriptError, check_script
from .lock import DeviceLock
from .workspace import Section, Workspace

if TYPE_CHECKING:
    from PIL.Image import Image

    from .shell import Shell


class Unsupported(Exception):
    """The target does not have this feature"""


@dataclass(frozen=True)
class Lcd:
    width: int
    height: int
    format: str = "rgb565"          # or "rgb565-swapped": bytes swapped in memory


@dataclass
class RunRequest:
    image: Path                     # usually the build's rockbox.elf
    seconds: int                    # the image returns to the host after this long
    keys: str = ""                  # key script (auto_rockbox.keys)
    settings: dict[str, str] = field(default_factory=dict)   # target-defined --set k=v
    cold: bool = False
    usb: bool = False


@dataclass
class Battery:
    millivolts: int
    charging: bool
    note: str = ""


class Target(ABC):
    name: ClassVar[str]
    summary: ClassVar[str]
    lcd: ClassVar[Lcd]
    key_letters: ClassVar[str]      # letters the firmware's key-script parser takes
    key_suffixes: ClassVar[str] = "+"
    keys_max: ClassVar[int] = 239   # longest key script the run config holds
    capabilities: ClassVar[frozenset[str]] = frozenset()
    return_margin: ClassVar[float] = 40.0   # seconds past req.seconds before "lost"

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self.conf: Section = ws.target(self.name)

    def lock(self) -> DeviceLock:
        return DeviceLock(self.name)

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def check_keys(self, script: str) -> None:
        if len(script.encode()) > self.keys_max:
            raise KeyScriptError(f"key script over {self.keys_max} bytes")
        check_script(script, self.key_letters, self.key_suffixes)

    # The dev loop: put the image on the player and start it, with the run
    # config (return time, keys, settings) where the image will find it
    @abstractmethod
    def launch(self, req: RunRequest) -> None: ...

    # Until the image is back in the player's flashing mode or DEADLINE
    # (time.monotonic()) passes; TICK about every 0.2 s. True if it returned.
    @abstractmethod
    def wait_return(self, deadline: float, tick: Callable[[], None]) -> bool: ...

    @abstractmethod
    def read_log(self) -> str: ...

    # Optional around a run: charger guards, reading back state
    def before_run(self, req: RunRequest) -> None:
        return None

    def after_run(self, req: RunRequest, returned: bool) -> None:
        return None

    def read_framebuffer(self, elf: Path | None) -> Image:
        raise Unsupported(f"{self.name}: no framebuffer read")

    def shell(self) -> Shell:
        raise Unsupported(f"{self.name}: no dev shell")

    def battery(self) -> Battery:
        raise Unsupported(f"{self.name}: no battery read")
