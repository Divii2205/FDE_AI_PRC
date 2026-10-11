from __future__ import annotations

import requests
from fastapi.testclient import TestClient

from mock_api.app import app

_client = TestClient(app)


def in_process_vendor_risk(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    """Same contract as src.vendor_client.get_vendor_risk, served by the mock app in-process."""
    response = _client.get(f"/vendor-risk/{vendor_name}")
    if response.status_code >= 400:
        fake = requests.Response()
        fake.status_code = response.status_code
        fake._content = response.content
        raise requests.HTTPError(f"{response.status_code}", response=fake)
    return response.json()


def vendor_api_down(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    raise requests.ConnectionError("connection refused")
