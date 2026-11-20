import os
import sys
from pathlib import Path

os.environ.setdefault("GOLD_EMBEDDED_WORKER", "0")  # the web app must not start background jobs inside tests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import socket

import numpy as np
import pandas as pd
import pytest

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def block_network(monkeypatch, message: str = "a test tried to use the network") -> None:
    """Make any real connection fail loudly. Loopback stays open: on Windows asyncio builds its own self-pipe from a socket pair
    connected over 127.0.0.1, so the in-process test client needs it; nothing there leaves the machine."""
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else address
        if host in _LOOPBACK:
            return real_connect(self, address, *a, **k)
        raise AssertionError(message)

    def no_lookup(*a, **k):
        raise AssertionError(message)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", no_lookup)


def make_series(n=900, seed=0, start="2021-01-04", drift=0.0002, vol=0.01) -> pd.Series:
    """Synthetic prices used ONLY inside tests (the app itself never fabricates data)."""
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(1800 * np.exp(np.cumsum(r)), index=idx, name="close")


@pytest.fixture
def series():
    return make_series()
