# Thin shim so pip's console_scripts mechanism has an importable target.
# main.py itself is written to run as a script (its own sys.path.insert
# finds "src" as a sibling of wherever it physically lives), not to be
# imported as a package submodule, so this loads the installed copy of
# main.py itself, then calls its main() directly. Not runpy.run_path: it
# unconditionally overwrites sys.argv[0] with the script's own file path
# right before executing (so --help/argparse errors would say "main.py"
# regardless of what this sets sys.argv[0] to beforehand), which
# importlib.util's spec/exec_module path doesn't do.
import importlib.util
import os
import sys


def run():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.argv[0] = "lapsecoin"
    spec = importlib.util.spec_from_file_location("_lapsecoin_main", os.path.join(here, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        mod.main()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
