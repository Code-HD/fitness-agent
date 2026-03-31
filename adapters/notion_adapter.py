"""Notion 스토리지 어댑터 — stub."""

from __future__ import annotations

from datetime import date
from typing import Optional

from adapters.base import StorageAdapter
from core.models import BodyMetrics, Workout, WorkoutCategory


class NotionAdapter(StorageAdapter):
    """추후 구현 예정. Notion API를 통한 데이터 저장/조회."""

    def __init__(self, api_key: str, database_ids: dict[str, str]):
        self.api_key = api_key
        self.database_ids = database_ids
        raise NotImplementedError("NotionAdapter는 아직 구현되지 않았습니다.")

    def save_workout(self, workout: Workout) -> int:
        raise NotImplementedError

    def get_workouts(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        category: Optional[WorkoutCategory] = None,
        name: Optional[str] = None,
        limit: int = 100,
    ) -> list[Workout]:
        raise NotImplementedError

    def get_workout_by_id(self, workout_id: int) -> Optional[Workout]:
        raise NotImplementedError

    def save_body_metrics(self, metrics: BodyMetrics) -> int:
        raise NotImplementedError

    def get_body_metrics(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100,
    ) -> list[BodyMetrics]:
        raise NotImplementedError

    def get_exercise_names(self, category: Optional[WorkoutCategory] = None) -> list[str]:
        raise NotImplementedError
