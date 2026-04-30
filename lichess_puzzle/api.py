"""Thin wrapper around the Lichess puzzle endpoints we use."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

BASE_URL = "https://lichess.org"


def _token() -> str:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    token = os.environ.get("LICHESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "LICHESS_TOKEN is missing. Copy .env.example to .env and put your "
            "Lichess personal token there (https://lichess.org/account/oauth/token)."
        )
    return token


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def fetch_next_puzzle() -> dict[str, Any]:
    """GET /api/puzzle/next — returns the full {game, puzzle} payload."""
    with httpx.Client(timeout=15.0) as client:
        r = client.get(f"{BASE_URL}/api/puzzle/next", headers=_headers())
        r.raise_for_status()
        return r.json()


def submit_result(puzzle_id: str, win: bool, rated: bool = False) -> dict[str, Any]:
    """POST /api/puzzle/batch/mix with a single solution."""
    payload = {"solutions": [{"id": puzzle_id, "win": win, "rated": rated}]}
    with httpx.Client(timeout=15.0) as client:
        r = client.post(
            f"{BASE_URL}/api/puzzle/batch/mix",
            headers={**_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
        return r.json()
