"""Make `custom_components.aquarea_home` importable without Home Assistant.

The package __init__ imports a handful of Home Assistant names; ha_stub
provides in-memory fakes for exactly those, installed before anything
imports the package. api.py and poll.py need no Home Assistant at all.
"""
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for path in (TESTS, TESTS.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import ha_stub  # noqa: E402

ha_stub.install()
