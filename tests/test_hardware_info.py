"""Unit tests for hardware_info.py: the odds page's "what's this node
running on" cell. Nothing here mocks the platform, it reads this actual
test machine, the point is confirming describe() never raises and always
returns the documented shape, not what any particular value happens to be.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import hardware_info  # noqa: E402


class TestDescribe:
    def test_returns_all_documented_keys(self):
        info = hardware_info.describe()
        assert set(info) == {"cpu", "cores", "ram_bytes", "os", "arch"}

    def test_cpu_is_a_nonempty_string(self):
        info = hardware_info.describe()
        assert isinstance(info["cpu"], str) and info["cpu"]

    def test_cores_is_a_positive_int_or_none(self):
        cores = hardware_info.describe()["cores"]
        assert cores is None or (isinstance(cores, int) and cores > 0)

    def test_ram_bytes_is_a_positive_int_or_none(self):
        ram = hardware_info.describe()["ram_bytes"]
        assert ram is None or (isinstance(ram, int) and ram > 0)

    def test_os_and_arch_are_nonempty_strings(self):
        info = hardware_info.describe()
        assert isinstance(info["os"], str) and info["os"]
        assert isinstance(info["arch"], str) and info["arch"]

    def test_never_raises_even_if_proc_files_are_unreadable(self, monkeypatch):
        """Linux-specific /proc reads are the one thing that could fail in
        an unusual sandbox; confirm the fallback path holds regardless."""
        real_open = open

        def _boom(path, *a, **k):
            if "/proc/" in str(path):
                raise OSError("no such file")
            return real_open(path, *a, **k)

        monkeypatch.setattr("builtins.open", _boom)
        info = hardware_info.describe()  # must not raise
        assert info["cpu"]  # falls back to platform.processor()/machine()
