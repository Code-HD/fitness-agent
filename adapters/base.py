"""스토리지 어댑터 추상 인터페이스 — 세션 기반."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Optional

from core.models import (
    BodyMetrics, CoachingPhase, Meal, NutritionTarget,
    Session, SessionType, SleepLog, WeeklyPlan,
)


class StorageAdapter(ABC):

    # --- Weekly Plan ---

    @abstractmethod
    def save_plan(self, plan: WeeklyPlan) -> int:
        """주간 계획 저장. ID 반환."""

    @abstractmethod
    def get_plan(self, year: int, week_number: int) -> Optional[WeeklyPlan]:
        """연도+주차로 계획 조회."""

    @abstractmethod
    def get_plans(self, limit: int = 10) -> list[WeeklyPlan]:
        """최근 주간 계획 목록."""

    # --- Session ---

    @abstractmethod
    def save_session(self, session: Session) -> int:
        """세션(운동+종목+세트 포함) 저장. ID 반환."""

    @abstractmethod
    def get_sessions(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        session_type: Optional[SessionType] = None,
        plan_id: Optional[int] = None,
        limit: int = 100,
    ) -> list[Session]:
        """조건에 맞는 세션 조회."""

    @abstractmethod
    def get_session_by_id(self, session_id: int) -> Optional[Session]:
        """ID로 세션 조회."""

    # --- Body Metrics ---

    @abstractmethod
    def save_body_metrics(self, metrics: BodyMetrics) -> int:
        """체성분 기록 저장."""

    @abstractmethod
    def get_body_metrics(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100,
    ) -> list[BodyMetrics]:
        """체성분 기록 조회."""

    # --- Utility ---

    @abstractmethod
    def get_exercise_names(self) -> list[str]:
        """기록된 운동명 목록."""

    # --- Nutrition ---

    @abstractmethod
    def save_meal(self, meal: Meal) -> int:
        """식사 기록 저장. ID 반환."""

    @abstractmethod
    def get_meals(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100,
    ) -> list[Meal]:
        """식사 기록 조회."""

    @abstractmethod
    def save_nutrition_target(self, target: NutritionTarget) -> int:
        """영양 목표 저장. ID 반환."""

    @abstractmethod
    def get_nutrition_target(self, target_date: Optional[date] = None) -> Optional[NutritionTarget]:
        """현재(또는 특정 날짜) 영양 목표 조회."""

    # --- Recovery ---

    @abstractmethod
    def save_sleep_log(self, log: SleepLog) -> int:
        """수면 기록 저장. ID 반환."""

    @abstractmethod
    def get_sleep_logs(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100,
    ) -> list[SleepLog]:
        """수면 기록 조회."""

    # --- Coaching State ---

    @abstractmethod
    def save_coaching_phase(self, phase: CoachingPhase) -> int:
        """코칭 페이즈 저장. ID 반환."""

    @abstractmethod
    def get_current_phase(self) -> Optional[CoachingPhase]:
        """현재 활성 코칭 페이즈 조회."""

    @abstractmethod
    def get_phase_history(self, limit: int = 10) -> list[CoachingPhase]:
        """코칭 페이즈 이력 조회."""
