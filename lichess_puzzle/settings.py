"""User-tunable settings for the puzzle overlay.

Loaded from one of (in priority order):
  1. $HML_CONFIG (explicit path)
  2. <cwd>/hml.json (project-local)
  3. ~/.config/hml/settings.json (user-level)
  4. Built-in defaults

Schema (Pydantic v2):
  size: preset name ("small"|"medium"|"large"|"xl"|"xxl") OR numeric scale
  position: preset ("top-right"|"top-left"|"bottom-right"|"bottom-left"|"center")
            OR a [row, col] tuple (1-indexed cells)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal, Union

from pydantic import BaseModel, ValidationError, field_validator


SIZE_PRESETS = {
    "small": 0.75,
    "medium": 1.00,
    "large": 1.25,
    "xl": 1.50,
    "xxl": 2.00,
}

PositionPreset = Literal[
    "top-right", "top-left", "bottom-right", "bottom-left", "center"
]


class Settings(BaseModel):
    size: float = 1.00
    position: Union[PositionPreset, tuple[int, int]] = "top-right"

    @field_validator("size", mode="before")
    @classmethod
    def _coerce_size(cls, v):
        if isinstance(v, str):
            if v not in SIZE_PRESETS:
                raise ValueError(
                    f"size: '{v}' must be one of {list(SIZE_PRESETS)} or a number"
                )
            return SIZE_PRESETS[v]
        v = float(v)
        if v <= 0:
            raise ValueError("size must be > 0")
        return v


def _candidate_paths() -> list[Path]:
    env = os.environ.get("HML_CONFIG")
    paths: list[Path] = []
    if env:
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / "hml.json")
    paths.append(Path.home() / ".config" / "hml" / "settings.json")
    return paths


def load_settings() -> Settings:
    for path in _candidate_paths():
        if not path.exists():
            continue
        try:
            return Settings.model_validate_json(path.read_text())
        except (ValidationError, OSError, ValueError) as e:
            print(f"hml: ignoring invalid settings at {path}: {e}", file=sys.stderr)
            return Settings()
    return Settings()
