from __future__ import annotations

import os
from urllib.parse import quote, urlparse
import requests

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_direct = requests.Session()
_direct.trust_env = False  # never send local calls through a system/corporate proxy


def get_vendor_risk(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    """Low-level API client. Decide yourself whether/how this becomes an agent tool."""
    base_url = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    url = f"{base_url}/vendor-risk/{quote(vendor_name, safe='')}"
    http = _direct if urlparse(base_url).hostname in _LOCAL_HOSTS else requests
    response = http.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()
