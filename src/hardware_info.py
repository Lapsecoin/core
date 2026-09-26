"""Best-effort local hardware summary, for the odds page's "what is this
node actually running on" cell.

Deliberately stdlib-only, no psutil or similar: this project otherwise
avoids adding a dependency just to read a CPU name, and every value here
degrades to "unknown" rather than raising, the odds page renders exactly
the same either way. Nothing here is sent to a peer or stored anywhere,
it is read fresh on every request and shown only to whoever is looking at
this node's own dashboard (see settings.SHOW_HARDWARE_DETAILS for the
operator's own switch to hide it).
"""

import os
import platform


def _cpu_model() -> str:
    """Best available CPU name. platform.processor() is often blank on
    Linux (it shells out to `uname -p`, which many distros don't fill
    in), so /proc/cpuinfo's "model name" line is tried first there, the
    same information Bitcoin Core-style benchmarks usually mean by this."""
    if platform.system() == "Linux":
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "unknown"


def _total_ram_bytes():
    """Total physical RAM, or None if it can't be read without a third-
    party dependency (anything but Linux, right now)."""
    if platform.system() == "Linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        # "MemTotal:       16384000 kB"
                        return int(line.split()[1]) * 1024
        except (OSError, IndexError, ValueError):
            pass
    return None


def describe() -> dict:
    """{"cpu": str, "cores": int|None, "ram_bytes": int|None,
    "os": str, "arch": str}. Every field is best-effort; a value this
    platform can't determine reads as None (ram_bytes, cores) or
    "unknown" (cpu), never raises."""
    return {
        "cpu": _cpu_model(),
        "cores": os.cpu_count(),
        "ram_bytes": _total_ram_bytes(),
        "os": f"{platform.system()} {platform.release()}".strip(),
        "arch": platform.machine() or "unknown",
    }
