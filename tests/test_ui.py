import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _isolation  # noqa: F401

import unittest

from src.ui import fit_bar, short_path


def width(ft):
    return sum(len(t) for _, t in ft)


class TestStatusBar(unittest.TestCase):
    L = [("b", " gemini-3.8-flash "), ("", "│ "), ("g", "12% / 1M "), ("", "│ "), ("c", "Admin/omni ")]
    R = [("y", "thinking: high ")]

    def test_never_wider_than_terminal(self):
        for w in (200, 120, 80, 60, 40, 30, 20, 10):
            self.assertLessEqual(width(fit_bar(self.L, self.R, w)), w, w)

    def test_fills_exact_width_when_roomy(self):
        self.assertEqual(width(fit_bar(self.L, self.R, 100)), 99)

    def test_right_side_dropped_before_left_truncated(self):
        text = "".join(t for _, t in fit_bar(self.L, self.R, 50))
        self.assertNotIn("thinking", text)
        self.assertIn("gemini", text)

    def test_short_path(self):
        self.assertEqual(short_path("abc", 10), "abc")
        self.assertEqual(len(short_path("a" * 50, 10)), 10)


if __name__ == "__main__":
    unittest.main()
