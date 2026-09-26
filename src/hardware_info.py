"""Best-effort local hardware summary, for the odds page's "what is this
node actually running on" cell.

Stdlib-only, no psutil. Never raises; a value this platform can't
determine reads as "unknown". Read fresh on every request, never sent to
a peer or stored (see settings.SHOW_HARDWARE_DETAILS to hide it).
"""

import platform


def _cpu_model() -> str:
    """platform.processor() is often blank on Linux (it shells out to
    `uname -p`), so /proc/cpuinfo's "model name" line is tried first."""
    if platform.system() == "Linux":
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "unknown"


def describe() -> dict:
    """{"cpu": str, "os": str}."""
    return {
        "cpu": _cpu_model(),
        "os": f"{platform.system()} {platform.release()}".strip(),
    }
