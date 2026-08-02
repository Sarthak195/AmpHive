"""Entry point for ``python -m amphive_flasher`` and the frozen EXE."""

import sys

from amphive_flasher.cli import main

if __name__ == "__main__":
    sys.exit(main())
