import os
import sys

# platform/services/control_plane  (parent of the tests/ dir)
#
# The service runs with its own directory as the working directory, so `app.py` imports
# `model_meta` and `metrics` as top-level modules. Its tests import them the same way, which
# only resolves if that directory is on sys.path. Without this the suite did not even
# collect — and nothing noticed, because until 2026-08-20 no gate ran it. Same idiom as
# platform/services/agent/tests/conftest.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
