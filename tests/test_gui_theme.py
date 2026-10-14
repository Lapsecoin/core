import os
import sys

import pytest

# Not every machine that runs the tests has Tk (a bare CI image does not).
tk = pytest.importorskip("tkinter")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import gui_theme as theme  # noqa: E402


class _Root:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on
        self.pending = []

    def _do(self, name, *args):
        if name == self.fail_on:
            raise tk.TclError("no such window")
        self.calls.append((name,) + args)

    def update_idletasks(self): self._do("update_idletasks")
    def attributes(self, *a): self._do("attributes", *a)
    def lift(self): self._do("lift")
    def focus_force(self): self._do("focus_force")
    def after(self, ms, fn): self.pending.append((ms, fn))


class TestBringToFront:
    """A window opened by the update helper, a background process, stays
    behind the others on Windows and only flashes its taskbar button."""

    def test_shows_on_top_then_drops_the_topmost_mark(self):
        root = _Root()
        theme.bring_to_front(root)
        names = [c[0] for c in root.calls]
        assert ("attributes", "-topmost", True) in root.calls
        assert "lift" in names and "focus_force" in names
        assert len(root.pending) == 1
        root.pending[0][1]()
        assert root.calls[-1] == ("attributes", "-topmost", False)

    def test_a_closed_window_is_not_an_error(self):
        for name in ("attributes", "lift", "focus_force"):
            theme.bring_to_front(_Root(fail_on=name))
        root = _Root(fail_on="attributes")
        theme._drop_topmost(root)
