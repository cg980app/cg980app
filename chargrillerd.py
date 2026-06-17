#!/usr/bin/env python3
"""chargrillerd - Char-Griller BBQ Monitor & Web Dashboard.

Thin entrypoint. The implementation lives in the `cgriller` package; see
cgriller/__init__.py for the module layout and protocol documentation.

    python3 chargrillerd.py [options]
"""

from cgriller.app import run

if __name__ == "__main__":
    run()
