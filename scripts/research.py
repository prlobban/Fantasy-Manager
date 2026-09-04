#!/usr/bin/env python
"""Entry point for the pre-draft research pass (§3.2). See agent/research.py.

    python scripts/research.py                    # the pool, resuming
    python scripts/research.py --limit 5          # a taste
    python scripts/research.py --refresh-top 15   # after the 10:00 order lock
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.research import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
