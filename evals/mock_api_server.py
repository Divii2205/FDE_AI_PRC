"""Start the mock vendor-risk API for an eval run if it is not already reachable."""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
_local = requests.Session()
_local.trust_env = False  # bypass system proxies for localhost


def ensure_mock_api(timeout_seconds: float = 60.0) -> None:
    base = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    try:
        if _local.get(f"{base}/health", timeout=1).ok:
            return
    except requests.RequestException:
        pass
    port = base.rsplit(":", 1)[-1]
    print(f"Mock vendor-risk API not reachable at {base}; starting it for this run ...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", "127.0.0.1", "--port", port],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    atexit.register(proc.terminate)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if _local.get(f"{base}/health", timeout=1).ok:
                return
        except requests.RequestException:
            time.sleep(0.25)
    raise RuntimeError(f"Mock vendor-risk API did not start at {base}")
