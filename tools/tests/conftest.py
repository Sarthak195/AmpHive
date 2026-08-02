"""Makes tools/fake_plug.py importable as a top-level `fake_plug` module from
these tests, regardless of the directory pytest is invoked from — e.g.
`pytest tools/tests` from the repo root, or `pytest .` from inside tools/.
Mirrors tools/p110_sim/tests/conftest.py's rationale: fake_plug.py has no
package __init__.py by design (it's meant to be run directly as
`python tools/fake_plug.py`, which relies on Python auto-adding the script's
own directory to sys.path) — this conftest just gives the test suite the
same path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
