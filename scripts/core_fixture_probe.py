"""CI-only diagnostics for the public valid fixture; never used with installation data."""

import runpy
import sys
from pathlib import Path

helper = runpy.run_path("/app/ha_syncapp/_validator_child.py", run_name="fixture_probe")
result = helper["_check"](Path(sys.argv[1]), "2026.9.1")
print("Public Core fixture diagnostic result:", result)
