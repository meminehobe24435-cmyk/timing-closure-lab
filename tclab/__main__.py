"""``py -3.12 -m tclab`` entry point."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
