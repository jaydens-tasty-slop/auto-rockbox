"""`arb bt-peer`: the commands are registered (the over-the-air HID one for a
player with a shell), the workspace's [bluetooth] settings reach them, and
a missing Bumble or setting is an error before anything runs."""

from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path
from unittest import mock

from auto_rockbox import cli, registry
from auto_rockbox.btpeer import BumbleMissing, need_bumble
from auto_rockbox.btpeer.hidkeys import SimButtons
from auto_rockbox.commands import btpeer
from auto_rockbox.registry import Context
from auto_rockbox.workspace import Workspace


def _workspace(toml: str = "") -> tuple[tempfile.TemporaryDirectory[str], Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "auto-rockbox.toml").write_text(toml)
    return tmp, root


def test_registered() -> None:
    registry.load_plugins(Workspace(Path.cwd()))
    host = registry.host_commands()
    for action in ("sink", "hid", "pcsink", "pcheadset"):
        assert f"bt-peer {action}" in host
    yp3 = registry.targets()["yp3"]
    air = registry.commands_for(yp3)["bt-peer hid"]
    assert air.fn is btpeer.hid_air_cmd and "shell" in air.needs


def test_settings_from_the_workspace() -> None:
    tmp, root = _workspace('[paths]\nbuild = "out"\n[bluetooth]\nsim_hci = "_:7000"\n'
                           "[bluetooth.sim_buttons]\nup = 16\npad_shift = 20\n")
    with tmp:
        ctx = Context(Workspace.find(root))
        assert btpeer._hci(ctx, None) == "_:7000"
        assert btpeer._hci(ctx, "127.0.0.1:1") == "127.0.0.1:1"
        assert btpeer._out(ctx, None, "bt-peer") == root / "out/bt-peer"
        assert btpeer._sim_buttons(ctx) == SimButtons(up=16, pad_shift=20)
    ctx = Context(Workspace(Path("/nonexistent")))
    assert btpeer._hci(ctx, None) == "127.0.0.1:6402"
    assert btpeer._sim_buttons(ctx) == SimButtons()


def _arb(root: Path, *argv: str) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = cli.main(["--workspace", str(root), *argv])
    return rc, err.getvalue()


def test_errors_before_anything_runs() -> None:
    tmp, root = _workspace()
    with tmp:
        with mock.patch("importlib.util.find_spec", return_value=None):
            try:
                need_bumble()
            except BumbleMissing as e:
                assert "uv sync --extra bt" in str(e)
            else:
                raise AssertionError("no BumbleMissing")
            rc, err = _arb(root, "bt-peer", "sink")
            assert rc == 1 and "uv sync --extra bt" in err
        rc, err = _arb(root, "bt-peer", "hid", "--kind", "keyboard", "--press", "up x")
        if "uv sync" not in err:                # with Bumble: the key is refused
            assert rc == 1 and "keyboard has no key x" in err, err
        rc, err = _arb(root, "bt-peer", "pcheadset")
        if "uv sync" not in err:                # with Bumble: no adapter configured
            assert rc == 2 and "[bluetooth] adapter" in err, err
