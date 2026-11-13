import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest


def make_series(n=900, seed=0, start="2021-01-04", drift=0.0002, vol=0.01) -> pd.Series:
    """Synthetic prices used ONLY inside tests (the app itself never fabricates data)."""
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(1800 * np.exp(np.cumsum(r)), index=idx, name="close")


@pytest.fixture
def series():
    return make_series()
