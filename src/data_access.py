"""Data helpers built on the standard library.

pandas was dropped from the runtime path: on Windows machines with Smart App Control /
Application Control enforced, its compiled pyarrow dependency can be blocked from loading,
and these small CSV snapshots only need simple lookups.
"""
from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def _convert(value: str):
    """CSV text -> int / float / None / str."""
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


@lru_cache(maxsize=None)
def _read_csv(name: str) -> tuple[dict, ...]:
    with (DATA_DIR / name).open(encoding="utf-8", newline="") as f:
        return tuple({k: _convert(v) for k, v in row.items()} for row in csv.DictReader(f))


def _rows(name: str) -> list[dict]:
    return [dict(r) for r in _read_csv(name)]  # copies, so callers can't mutate the cache


def load_employees() -> list[dict]:
    return _rows("employees.csv")


def load_budgets() -> list[dict]:
    return _rows("department_budgets.csv")


def load_software_catalog() -> list[dict]:
    return _rows("software_catalog.csv")


def load_vendors() -> list[dict]:
    return _rows("vendors.csv")


def load_purchase_history() -> list[dict]:
    return _rows("purchase_history.csv")


def find_row(rows: list[dict], column: str, value) -> dict | None:
    """First row whose `column` equals `value` (case-insensitive), or None."""
    if value is None:
        return None
    target = str(value).casefold()
    return next((r for r in rows if str(r.get(column)).casefold() == target), None)


def load_requests() -> list[dict]:
    return json.loads((DATA_DIR / "requests.json").read_text(encoding="utf-8"))


def get_request(request_id: str) -> dict:
    for request in load_requests():
        if request["request_id"] == request_id:
            return request
    raise KeyError(f"Unknown request_id: {request_id}")


def load_policy_text() -> str:
    return (DATA_DIR / "procurement_policy.md").read_text(encoding="utf-8")
