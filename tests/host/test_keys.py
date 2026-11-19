"""Key-script checks (keys.py): touch tokens"""

from __future__ import annotations

import unittest

from auto_rockbox.keys import KeyScriptError, check_script, duration_ms

LETTERS = "BSMVvP"
PANEL = (240, 320)


class TouchTokens(unittest.TestCase):
    def test_pass_with_a_panel(self) -> None:
        for script in ("@0:0", "@239:319", "9000 @120:300>120:40", "@120:300>120:40+",
                       "@10:10* S @5:5~, M"):
            check_script(script, LETTERS, panel=PANEL)

    def test_bad_ones_fail(self) -> None:
        for script in ("@240:0", "@0:320", "@1:1>1:400", "@1", "@1:", "@1:2>", "@1:2S",
                       "@-1:2"):
            with self.subTest(script=script), self.assertRaises(KeyScriptError):
                check_script(script, LETTERS, panel=PANEL)

    def test_need_a_touchscreen(self) -> None:
        with self.assertRaisesRegex(KeyScriptError, "without a touchscreen"):
            check_script("@1:2", LETTERS)

    def test_last_as_long_as_keys(self) -> None:
        self.assertEqual(duration_ms("@1:2"), duration_ms("S"))
        self.assertEqual(duration_ms("@1:2>3:4++"), duration_ms("S++"))


if __name__ == "__main__":
    unittest.main()
