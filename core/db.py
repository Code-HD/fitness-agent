"""DB 추상화 레이어 — 어댑터를 통한 데이터 접근 진입점."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from adapters.base import StorageAdapter
from adapters.sqlite_adapter import SQLiteAdapter

_adapter: Optional[StorageAdapter] = None

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "fitness.db"


def get_adapter() -> StorageAdapter:
    global _adapter
    if _adapter is None:
        _adapter = SQLiteAdapter(DEFAULT_DB_PATH)
    return _adapter


def set_adapter(adapter: StorageAdapter) -> None:
    global _adapter
    _adapter = adapter
