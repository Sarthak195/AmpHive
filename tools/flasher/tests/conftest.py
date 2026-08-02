"""Make `amphive_flasher` importable without installing the package.

tools/flasher isn't pip-installed anywhere (it's a standalone tool, not part
of the backend's dependency tree) so tests need tools/flasher/ itself on
sys.path — this file, discovered automatically by pytest for every test in
this directory, does that.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
