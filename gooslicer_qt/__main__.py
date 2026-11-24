import sys

from .main import main

if __name__ == "__main__":  # guard: slicer worker processes re-import __main__
    sys.exit(main())
