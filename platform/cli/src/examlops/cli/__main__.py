"""``python -m examlops.cli`` — run `exa` without its console-script shim.

Where `examlops` is importable but not installed (the dashboard container puts its source on
``PYTHONPATH``), there is no ``exa`` executable on ``PATH``; this is the same entry point.
"""

from examlops.cli.main import app

app(prog_name="exa")
