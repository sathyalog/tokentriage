"""Loaded by Python at interpreter start-up when `tokentriage run` puts this folder on PYTHONPATH.

Enables tokentriage in a program without editing it; settings come from tokentriage.yaml and the
TOKENTRIAGE_ENABLED / _BACKEND / _MODE / _LOG_LEVEL variables. Any sitecustomize the environment already had is still run.
"""

import os
import runpy
import sys

_here = os.path.dirname(os.path.abspath(__file__))

# Chain to the environment's own sitecustomize, if any (ours shadows it on sys.path).
for _entry in sys.path:
    if not _entry or os.path.abspath(_entry) == _here:
        continue
    _candidate = os.path.join(_entry, "sitecustomize.py")
    if os.path.isfile(_candidate):
        runpy.run_path(_candidate, run_name="sitecustomize")
        break

if os.environ.get("TOKENTRIAGE_AUTOENABLE") == "1":
    try:
        import tokentriage

        tokentriage.enable()
    except Exception as exc:  # never stop the user's program from starting
        print(f"tokentriage: not enabled ({type(exc).__name__}: {exc})", file=sys.stderr)
