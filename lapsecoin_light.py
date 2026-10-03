# Entry point for the light client (the lapsecoin-dumb binary). Like main.py
# it is a script: it finds "src" next to wherever it physically lives.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "src"))

from light import main

if __name__ == "__main__":
    _sys.exit(main())
