"""Learned effects, peer groups, prediction and optimisation over the market panel.

Nothing in here imports runners.true_model or reads data/true_model.json. Everything is
estimated from the observable tables (restaurants, restaurant_weeks, change_events).
"""

from __future__ import annotations

import os
from pathlib import Path

MODELS_DIR = Path(os.environ.get("JET_MODELS_DIR", "data/models"))
