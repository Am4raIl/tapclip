"""Ponto de entrada para `python -m replaycam`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
