"""수치 분석 로직 — 인사이트 엔진 통합.

기존 AnalysisResult는 AI 컨텍스트용으로 유지.
InsightEngine의 구조화된 인사이트와 연동.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from core.models import BodyMetrics, Exercise, Session, SessionType
from core.insight_engine import (
    InsightEngine,
    SessionInsight,
    WeeklyInsight,
    PeriodInsight,
    classify_muscle_group,
)


@dataclass
class WeeklyVolume:
    week_start: date
    total_volume: float
    session_count: int
    exercises: dict[str, float] = field(default_factory=dict)


@dataclass
class ExerciseProgress:
    name: str
    period_days: int
    first_estimated_1rm: float
    last_estimated_1rm: float
    best_estimated_1rm: float
    volume_trend: list[WeeklyVolume]
    stagnation_weeks: int
    suggestion: Optional[str] = None


@dataclass
class OverloadAlert:
    current_week_volume: float
    previous_week_volume: float
    increase_pct: float
    exercise_name: Optional[str] = None


@dataclass
class BodyCompositionTrend:
    recent_weight: float
    weight_change_weekly: Optional[float]
    weight_change_monthly: Optional[float]
    body_fat_change: Optional[float]
    volume_vs_weight: Optional[str]


@dataclass
class AnalysisResult:
    period_days: int
    weekly_volumes: list[WeeklyVolume]
    exercise_progress: list[ExerciseProgress]
    overload_alerts: list[OverloadAlert]
    body_trend: Optional[BodyCompositionTrend]
    total_sessions: int
    session_breakdown: dict[str, int]
    # 인사이트 엔진 결과
    period_insight: Optional[PeriodInsight] = None
    muscle_group_volumes: Optional[dict[str, float]] = None


class Analyzer:
    OVERLOAD_THRESHOLD = 0.10

    def __init__(self) -> None:
        self.engine = InsightEngine()

    def analyze(
        self,
        sessions: list[Session],
        body_metrics: list[BodyMetrics],
        period_days: int = 30,
    ) -> AnalysisResult:
        weekly_vols = self._calc_weekly_volumes(sessions)
        progress = self._calc_exercise_progress(sessions, period_days)
        alerts = self._check_overload(weekly_vols)
        body_trend = self._analyze_body(body_metrics, weekly_vols)

        breakdown: dict[str, int] = defaultdict(int)
        for s in sessions:
            breakdown[s.session_type.value] += 1

        # 인사이트 엔진으로 기간 인사이트 생성
        period_insight = self.engine.period_insight(sessions, body_metrics, period_days)

        # 근육군 볼륨
        muscle_vols = self._calc_muscle_group_volumes(sessions)

        return AnalysisResult(
            period_days=period_days,
            weekly_volumes=weekly_vols,
            exercise_progress=progress,
            overload_alerts=alerts,
            body_trend=body_trend,
            total_sessions=len(sessions),
            session_breakdown=dict(breakdown),
            period_insight=period_insight,
            muscle_group_volumes=muscle_vols,
        )

    def session_insight(
        self,
        current_session: Session,
        all_history: list[Session],
    ) -> SessionInsight:
        """세션 직후 인사이트 (InsightEngine 위임)."""
        return self.engine.session_insight(current_session, all_history)

    def weekly_insight(
        self,
        week_sessions: list[Session],
        prev_week_sessions: list[Session],
        all_history: list[Session],
        body_metrics: list[BodyMetrics] | None = None,
    ) -> WeeklyInsight:
        """주간 인사이트 (InsightEngine 위임)."""
        return self.engine.weekly_insight(
            week_sessions, prev_week_sessions, all_history, body_metrics,
        )

    def _week_start(self, d: date) -> date:
        return d - timedelta(days=d.weekday())

    def _calc_weekly_volumes(self, sessions: list[Session]) -> list[WeeklyVolume]:
        weeks: dict[date, WeeklyVolume] = {}
        for s in sessions:
            ws = self._week_start(s.date)
            if ws not in weeks:
                weeks[ws] = WeeklyVolume(week_start=ws, total_volume=0, session_count=0)
            wv = weeks[ws]
            wv.session_count += 1
            for ex in s.exercises:
                vol = ex.total_volume
                wv.total_volume += vol
                if ex.name not in wv.exercises:
                    wv.exercises[ex.name] = 0
                wv.exercises[ex.name] += vol
        return sorted(weeks.values(), key=lambda v: v.week_start)

    def _calc_exercise_progress(
        self, sessions: list[Session], period_days: int
    ) -> list[ExerciseProgress]:
        by_name: dict[str, list[tuple[date, Exercise]]] = defaultdict(list)
        for s in sessions:
            for ex in s.exercises:
                if ex.is_weighted:
                    by_name[ex.name].append((s.date, ex))

        results = []
        for name, entries in by_name.items():
            entries.sort(key=lambda x: x[0])

            e1rms = [ex.top_set.estimated_1rm for _, ex in entries if ex.top_set]
            if not e1rms:
                continue

            # 정체 감지
            weekly_tops: dict[date, float] = {}
            for d, ex in entries:
                wk = self._week_start(d)
                top = ex.top_set
                if top:
                    weekly_tops[wk] = max(weekly_tops.get(wk, 0), top.weight_kg)

            stag = 0
            sorted_weeks = sorted(weekly_tops.items(), key=lambda x: x[0], reverse=True)
            if len(sorted_weeks) >= 2:
                ref = sorted_weeks[0][1]
                for _, wt in sorted_weeks[1:]:
                    if abs(wt - ref) < 0.1:
                        stag += 1
                    else:
                        break

            suggestion = None
            if stag >= 3:
                suggestion = f"{name}: 3주 이상 동일 중량. 중량 증가 또는 볼륨 변화를 고려하세요."
            elif stag >= 2:
                suggestion = f"{name}: 2주 동일 중량. 다음 세션에서 중량 시도 권장."

            ex_sessions = []
            for s in sessions:
                for ex in s.exercises:
                    if ex.name == name:
                        fake = Session(date=s.date, session_type=s.session_type, exercises=[ex])
                        ex_sessions.append(fake)
                        break
            vol_trend = self._calc_weekly_volumes(ex_sessions)

            results.append(ExerciseProgress(
                name=name, period_days=period_days,
                first_estimated_1rm=e1rms[0], last_estimated_1rm=e1rms[-1],
                best_estimated_1rm=max(e1rms),
                volume_trend=vol_trend, stagnation_weeks=stag, suggestion=suggestion,
            ))
        return results

    def _check_overload(self, weekly_volumes: list[WeeklyVolume]) -> list[OverloadAlert]:
        alerts = []
        if len(weekly_volumes) < 2:
            return alerts
        curr, prev = weekly_volumes[-1], weekly_volumes[-2]
        if prev.total_volume > 0:
            pct = (curr.total_volume - prev.total_volume) / prev.total_volume
            if pct > self.OVERLOAD_THRESHOLD:
                alerts.append(OverloadAlert(
                    current_week_volume=curr.total_volume,
                    previous_week_volume=prev.total_volume,
                    increase_pct=pct * 100,
                ))
        for name in curr.exercises:
            if name in prev.exercises and prev.exercises[name] > 0:
                ep = (curr.exercises[name] - prev.exercises[name]) / prev.exercises[name]
                if ep > self.OVERLOAD_THRESHOLD:
                    alerts.append(OverloadAlert(
                        current_week_volume=curr.exercises[name],
                        previous_week_volume=prev.exercises[name],
                        increase_pct=ep * 100, exercise_name=name,
                    ))
        return alerts

    def _analyze_body(
        self, body_metrics: list[BodyMetrics], weekly_volumes: list[WeeklyVolume],
    ) -> Optional[BodyCompositionTrend]:
        if not body_metrics:
            return None
        bm = sorted(body_metrics, key=lambda m: m.date)
        latest = bm[-1]

        weekly_change = None
        if len(bm) >= 2:
            prev = [m for m in bm[:-1] if (latest.date - m.date).days <= 10]
            if prev:
                weekly_change = latest.weight_kg - prev[0].weight_kg

        monthly_change = None
        month = [m for m in bm if 25 <= (latest.date - m.date).days <= 35]
        if month:
            monthly_change = latest.weight_kg - month[0].weight_kg

        bf_change = None
        with_bf = [m for m in bm if m.body_fat_pct is not None]
        if len(with_bf) >= 2:
            bf_change = with_bf[-1].body_fat_pct - with_bf[0].body_fat_pct

        vol_vs_weight = None
        if weekly_change is not None and len(weekly_volumes) >= 2:
            vdiff = weekly_volumes[-1].total_volume - weekly_volumes[-2].total_volume
            if weekly_change > 0.5 and vdiff > 0:
                vol_vs_weight = "체중↑ + 볼륨↑: 벌크업 진행 중"
            elif weekly_change < -0.5 and vdiff >= 0:
                vol_vs_weight = "체중↓ + 볼륨 유지/↑: 효과적 컷팅"
            elif weekly_change < -0.5 and vdiff < 0:
                vol_vs_weight = "체중↓ + 볼륨↓: 근손실 위험 — 단백질 확인"
            elif abs(weekly_change) <= 0.5:
                vol_vs_weight = "체중 유지 중"

        return BodyCompositionTrend(
            recent_weight=latest.weight_kg,
            weight_change_weekly=weekly_change,
            weight_change_monthly=monthly_change,
            body_fat_change=bf_change,
            volume_vs_weight=vol_vs_weight,
        )

    def _calc_muscle_group_volumes(self, sessions: list[Session]) -> dict[str, float]:
        """근육군별 볼륨 합계."""
        groups: dict[str, float] = defaultdict(float)
        for s in sessions:
            for ex in s.exercises:
                group = classify_muscle_group(ex.name)
                groups[group] += ex.total_volume
        return dict(groups)
