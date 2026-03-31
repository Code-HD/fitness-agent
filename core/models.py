"""Fitness Agent 데이터 모델 — 세션 기반 통합 구조.

핵심 구조:
  오운동 시리즈 > 주간Plan (Week N) > 세션 (오운동-1, 더리셋 등) > 운동 > 세트

한 세션에 웨이트, 유산소, 필라테스가 혼합될 수 있음.
세트 기록은 유동적: 중량×횟수, 시간(초), 맨몸 횟수, 양측 구분 등.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class SessionType(str, Enum):
    WORKOUT = "workout"     # 오운동-N
    RESET = "reset"         # 더리셋 (필라테스)


@dataclass
class ExerciseSet:
    """운동 1세트 — 유동적 기록."""
    set_number: int
    weight_kg: Optional[float] = None
    reps: Optional[int] = None
    duration_sec: Optional[float] = None
    side: Optional[str] = None          # R, L, N
    rpe: Optional[float] = None         # 1-10
    note: str = ""
    id: Optional[int] = None
    exercise_id: Optional[int] = None

    @property
    def volume(self) -> float:
        if self.weight_kg and self.reps:
            return self.weight_kg * self.reps
        return 0.0

    @property
    def estimated_1rm(self) -> float:
        """Epley formula."""
        if not self.weight_kg or not self.reps:
            return 0.0
        if self.reps == 1:
            return self.weight_kg
        return self.weight_kg * (1 + self.reps / 30)


@dataclass
class Exercise:
    """세션 내 개별 운동 종목."""
    name: str
    order: int
    sets: list[ExerciseSet] = field(default_factory=list)
    note: str = ""
    id: Optional[int] = None
    session_id: Optional[int] = None

    @property
    def total_volume(self) -> float:
        return sum(s.volume for s in self.sets)

    @property
    def top_set(self) -> Optional[ExerciseSet]:
        weighted = [s for s in self.sets if s.weight_kg and s.reps]
        return max(weighted, key=lambda s: s.estimated_1rm) if weighted else None

    @property
    def is_timed(self) -> bool:
        return any(s.duration_sec is not None for s in self.sets)

    @property
    def is_weighted(self) -> bool:
        return any(s.weight_kg is not None for s in self.sets)


@dataclass
class Session:
    """운동 세션 — 오운동-N 또는 더리셋."""
    date: date
    session_type: SessionType
    session_number: Optional[int] = None
    exercises: list[Exercise] = field(default_factory=list)
    note: str = ""
    id: Optional[int] = None
    plan_id: Optional[int] = None
    created_at: Optional[datetime] = None

    @property
    def label(self) -> str:
        if self.session_type == SessionType.RESET:
            return "더리셋"
        return f"오운동-{self.session_number}"

    @property
    def total_volume(self) -> float:
        return sum(e.total_volume for e in self.exercises)

    @property
    def day_label(self) -> str:
        days = ["월", "화", "수", "목", "금", "토", "일"]
        return days[self.date.weekday()]


@dataclass
class WeeklyPlan:
    """주간 계획 — Week N."""
    year: int
    week_number: int
    sessions: list[Session] = field(default_factory=list)
    note: str = ""
    id: Optional[int] = None
    created_at: Optional[datetime] = None

    @property
    def label(self) -> str:
        return f"오운동 주간Plan - Week {self.week_number}"


@dataclass
class BodyMetrics:
    """체성분 기록."""
    date: date
    weight_kg: float
    body_fat_pct: Optional[float] = None
    skeletal_muscle_kg: Optional[float] = None
    bmi: Optional[float] = None
    body_fat_kg: Optional[float] = None
    note: str = ""
    id: Optional[int] = None
    created_at: Optional[datetime] = None


# --- 파싱 유틸리티 ---

def parse_sets(sets_str: str, rpe: Optional[float] = None) -> list[ExerciseSet]:
    """유동적 세트 문자열 파싱.

    지원 형식:
      '100x5, 105x3'                    → 중량×횟수
      '42s, 27s, 20s'                   → 시간(초)
      '8min'                            → 시간(분→초)
      '7, 6, 5'                         → 맨몸 횟수
      '15kg 10&10(R), 15kg 8&8(L)'     → 양측
      '100x5 @8'                        → RPE 포함
    """
    result = []
    parts = [p.strip() for p in sets_str.split(",")]

    for i, part in enumerate(parts, start=1):
        s = ExerciseSet(set_number=i, rpe=rpe)

        # RPE: @8
        if "@" in part:
            part, rpe_str = part.rsplit("@", 1)
            part = part.strip()
            try:
                s.rpe = float(rpe_str.strip())
            except ValueError:
                pass

        # 양측: (R), (L), (N)
        for marker, side in {"(r)": "R", "(l)": "L", "(n)": "N"}.items():
            if marker in part.lower():
                s.side = side
                part = part.lower().replace(marker, "").strip()
                break

        pl = part.lower().strip()

        # 시간(분): 8min
        if pl.endswith("min"):
            try:
                s.duration_sec = float(pl[:-3].strip()) * 60
                result.append(s)
                continue
            except ValueError:
                pass

        # 시간(초): 42s
        if pl.endswith("s") and "x" not in pl and "kg" not in pl:
            try:
                s.duration_sec = float(pl[:-1].strip())
                result.append(s)
                continue
            except ValueError:
                pass

        # 중량×횟수: 100x5
        if "x" in pl and "kg" not in pl:
            try:
                w, r = pl.split("x", 1)
                s.weight_kg = float(w.strip())
                s.reps = int(r.strip())
                result.append(s)
                continue
            except ValueError:
                pass

        # kg + 양측: 15kg 10&10
        if "kg" in pl and "&" in pl:
            try:
                kg_part, reps_part = pl.split("kg", 1)
                s.weight_kg = float(kg_part.strip())
                rep_vals = reps_part.strip().split("&")
                s.reps = sum(int(r.strip()) for r in rep_vals)
                s.note = "&".join(r.strip() for r in rep_vals)
                result.append(s)
                continue
            except ValueError:
                pass

        # kgx횟수: 25kgx13
        if "kg" in pl:
            try:
                if "x" in pl:
                    kg_part, r = pl.split("x", 1)
                    s.weight_kg = float(kg_part.replace("kg", "").strip())
                    s.reps = int(r.strip())
                else:
                    nums = pl.replace("kg", " ").split()
                    s.weight_kg = float(nums[0])
                    if len(nums) > 1:
                        s.reps = int(nums[1])
                result.append(s)
                continue
            except (ValueError, IndexError):
                pass

        # 맨몸 횟수: 7
        try:
            s.reps = int(part.strip())
            result.append(s)
            continue
        except ValueError:
            pass

        # 파싱 불가 → note
        s.note = part.strip()
        result.append(s)

    return result
