"""인사이트 엔진 — 세션/주간/기간별 3단계 자동 분석.

사용자는 운동하고 기록한다. 에이전트는 관리하고 분석한다.
인사이트는 이 에이전트의 핵심 가치.

3단계:
  1. 세션 인사이트: 매 기록 직후 자동 — PR, 볼륨 비교, 피로도, 강도
  2. 주간 인사이트: 주간 리포트 — 볼륨 추이, 균형, 회복, 일관성
  3. 기간 인사이트: 요청 시 — 장기 추세, 정체기, 주기화, 체성분 상관
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from core.models import BodyMetrics, Exercise, ExerciseSet, Session, SessionType


# ─── 근육군 분류 ───

_GROUP_KEYWORDS: dict[str, list[str]] = {
    "push": [
        "벤치", "프레스", "ohp", "숄더", "딥스", "푸쉬업", "체스트",
        "삼두", "트라이", "인클라인", "디클라인", "플라이", "푸시업",
    ],
    "pull": [
        "데드리프트", "랫풀", "풀다운", "로우", "풀업", "턱걸이",
        "이두", "바이셉", "시티드", "페이스풀", "원암", "바벨로우",
    ],
    "legs": [
        "스쿼트", "레그", "런지", "힙쓰러스트", "카프", "종아리",
        "글루트", "불가리안", "레그프레스", "레그컬", "레그익스텐션",
    ],
    "core": [
        "플랭크", "크런치", "시트업", "데드행", "행잉", "복근",
        "사이드밴드", "ab", "코어",
    ],
}


def classify_muscle_group(exercise_name: str) -> str:
    name_lower = exercise_name.lower()
    for group, keywords in _GROUP_KEYWORDS.items():
        for kw in keywords:
            if kw in name_lower:
                return group
    return "other"


# ─── 데이터 구조 ───


@dataclass
class PersonalRecord:
    exercise_name: str
    record_type: str       # "1rm", "max_weight", "max_reps", "max_volume", "max_duration"
    new_value: float
    previous_best: float
    improvement: float     # absolute
    improvement_pct: float
    achieved_date: date


@dataclass
class FatigueIndicator:
    exercise_name: str
    first_set_performance: float
    last_set_performance: float
    decline_pct: float
    assessment: str  # "정상", "높음", "과도"


@dataclass
class VolumeComparison:
    exercise_name: str
    current_volume: float
    previous_volume: float
    change_pct: float
    days_since_last: int


@dataclass
class IntensityScore:
    """세션 강도 점수 (0-100)."""
    score: float
    components: dict[str, float]  # volume_score, intensity_score, density_score
    label: str  # "저강도", "중강도", "고강도", "최대강도"


@dataclass
class SessionInsight:
    """세션 직후 자동 인사이트."""
    session_label: str
    session_date: date
    prs: list[PersonalRecord]
    volume_comparisons: list[VolumeComparison]
    fatigue_indicators: list[FatigueIndicator]
    intensity: IntensityScore
    highlights: list[str]
    warnings: list[str]
    total_volume: float
    exercise_count: int
    set_count: int


@dataclass
class MuscleGroupBalance:
    push_volume: float
    pull_volume: float
    legs_volume: float
    core_volume: float
    other_volume: float
    push_pct: float
    pull_pct: float
    legs_pct: float
    assessment: str


@dataclass
class RecoveryPattern:
    avg_rest_days: float
    min_rest_days: int
    max_rest_days: int
    consecutive_training_days: int
    assessment: str


@dataclass
class WeeklyInsight:
    """주간 인사이트."""
    week_label: str
    total_volume: float
    prev_week_volume: float
    volume_change_pct: float
    workout_count: int
    reset_count: int
    unique_exercises: int
    muscle_balance: MuscleGroupBalance
    recovery: RecoveryPattern
    prs: list[PersonalRecord]
    top_exercises: list[tuple[str, float]]  # (name, volume)
    highlights: list[str]
    recommendations: list[str]


@dataclass
class ExerciseTrend:
    name: str
    data_points: int
    first_1rm: float
    latest_1rm: float
    best_1rm: float
    trend_direction: str     # "상승", "하락", "정체"
    weekly_change_rate: float  # avg weekly 1RM change
    stagnation_weeks: int
    total_volume: float
    avg_session_volume: float


@dataclass
class PeriodInsight:
    """기간별 종합 인사이트."""
    period_days: int
    total_sessions: int
    total_volume: float
    avg_weekly_volume: float
    avg_weekly_sessions: float
    exercise_trends: list[ExerciseTrend]
    plateaus: list[str]
    improving_exercises: list[str]
    declining_exercises: list[str]
    body_summary: Optional[str]
    consistency_pct: float
    top_achievements: list[str]
    focus_areas: list[str]
    action_items: list[str]


# ─── 인사이트 엔진 ───


class InsightEngine:
    """3단계 자동 인사이트 엔진."""

    def session_insight(
        self,
        current_session: Session,
        all_history: list[Session],
    ) -> SessionInsight:
        """세션 직후 인사이트. all_history는 현재 세션 포함 전체 기록."""

        # 현재 세션 제외한 이전 기록
        history = [s for s in all_history if s.id != current_session.id]

        prs = self._detect_prs(current_session, history)
        vol_comps = self._compare_volumes(current_session, history)
        fatigue = self._detect_fatigue(current_session)
        intensity = self._calc_intensity(current_session, history)

        highlights = []
        warnings = []

        # PR 하이라이트
        for pr in prs:
            if pr.record_type == "1rm":
                highlights.append(
                    f"🏆 {pr.exercise_name} 1RM 신기록! "
                    f"{pr.previous_best:.1f} → {pr.new_value:.1f}kg "
                    f"(+{pr.improvement:.1f}kg)"
                )
            elif pr.record_type == "max_weight":
                highlights.append(
                    f"🏆 {pr.exercise_name} 최고중량! {pr.new_value:.1f}kg"
                )
            elif pr.record_type == "max_volume":
                highlights.append(
                    f"📈 {pr.exercise_name} 세션 볼륨 신기록! "
                    f"{pr.new_value:,.0f}kg (+{pr.improvement_pct:.0f}%)"
                )
            elif pr.record_type == "max_duration":
                highlights.append(
                    f"⏱️ {pr.exercise_name} 최장 시간! {pr.new_value:.0f}초"
                )

        # 볼륨 변화 하이라이트
        for vc in vol_comps:
            if vc.change_pct > 20:
                highlights.append(
                    f"📈 {vc.exercise_name} 볼륨 +{vc.change_pct:.0f}% "
                    f"(vs {vc.days_since_last}일 전)"
                )
            elif vc.change_pct < -20:
                warnings.append(
                    f"📉 {vc.exercise_name} 볼륨 {vc.change_pct:.0f}% "
                    f"(vs {vc.days_since_last}일 전)"
                )

        # 피로도 경고
        for fi in fatigue:
            if fi.assessment == "과도":
                warnings.append(
                    f"⚠️ {fi.exercise_name} 세트 간 퍼포먼스 급락 "
                    f"(-{fi.decline_pct:.0f}%) — 과도한 피로"
                )
            elif fi.assessment == "높음":
                warnings.append(
                    f"💪 {fi.exercise_name} 피로도 높음 "
                    f"(-{fi.decline_pct:.0f}%) — 컨디션 체크"
                )

        # 강도 하이라이트
        if intensity.score >= 80:
            highlights.append(f"🔥 고강도 세션 (강도 {intensity.score:.0f}/100)")
        elif intensity.score <= 30:
            highlights.append(f"🌱 가벼운 세션 (강도 {intensity.score:.0f}/100)")

        # 연속 훈련 경고
        recent_dates = sorted(set(s.date for s in history), reverse=True)
        consecutive = 0
        check_date = current_session.date - timedelta(days=1)
        for d in recent_dates:
            if d == check_date:
                consecutive += 1
                check_date -= timedelta(days=1)
            else:
                break
        if consecutive >= 3:
            warnings.append(
                f"🛑 {consecutive + 1}일 연속 훈련 — 휴식 권장"
            )
        elif consecutive == 2:
            warnings.append(
                f"⚡ 3일 연속 훈련 — 내일 회복일 고려"
            )

        set_count = sum(len(ex.sets) for ex in current_session.exercises)

        return SessionInsight(
            session_label=current_session.label,
            session_date=current_session.date,
            prs=prs,
            volume_comparisons=vol_comps,
            fatigue_indicators=fatigue,
            intensity=intensity,
            highlights=highlights,
            warnings=warnings,
            total_volume=current_session.total_volume,
            exercise_count=len(current_session.exercises),
            set_count=set_count,
        )

    def weekly_insight(
        self,
        week_sessions: list[Session],
        prev_week_sessions: list[Session],
        all_history: list[Session],
        body_metrics: list[BodyMetrics] | None = None,
    ) -> WeeklyInsight:
        """주간 인사이트."""

        curr_vol = sum(s.total_volume for s in week_sessions)
        prev_vol = sum(s.total_volume for s in prev_week_sessions)
        vol_change = ((curr_vol - prev_vol) / prev_vol * 100) if prev_vol > 0 else 0

        wk_count = sum(1 for s in week_sessions if s.session_type == SessionType.WORKOUT)
        rs_count = sum(1 for s in week_sessions if s.session_type == SessionType.RESET)

        # 운동 종류
        all_ex_names = set()
        for s in week_sessions:
            for ex in s.exercises:
                all_ex_names.add(ex.name)

        # 근육군 균형
        balance = self._calc_muscle_balance(week_sessions)

        # 회복 패턴
        recovery = self._calc_recovery(week_sessions)

        # 이번 주 PR
        # 이전 전체 기록(이번 주 제외)에서 비교
        prs = []
        week_date_set = set(s.id for s in week_sessions)
        history_before = [s for s in all_history if s.id not in week_date_set]
        for sess in week_sessions:
            session_prs = self._detect_prs(sess, history_before)
            prs.extend(session_prs)
            # 이 세션을 history에 추가 (다음 세션 PR 비교용)
            history_before.append(sess)

        # 상위 운동 (볼륨 기준)
        ex_volumes: dict[str, float] = defaultdict(float)
        for s in week_sessions:
            for ex in s.exercises:
                ex_volumes[ex.name] += ex.total_volume
        top_exercises = sorted(ex_volumes.items(), key=lambda x: x[1], reverse=True)[:5]

        # 하이라이트 & 추천
        highlights = []
        recommendations = []

        if prs:
            highlights.append(f"🏆 이번 주 신기록 {len(prs)}개!")
        if vol_change > 10:
            highlights.append(f"📈 주간 볼륨 +{vol_change:.0f}% 증가")
        elif vol_change < -10:
            highlights.append(f"📉 주간 볼륨 {vol_change:.0f}% 감소")
        if wk_count >= 4:
            highlights.append(f"💪 이번 주 {wk_count}회 훈련 — 높은 빈도!")
        if rs_count > 0:
            highlights.append(f"🧘 더리셋 {rs_count}회 — 회복 관리 Good")

        # 균형 추천
        if balance.assessment:
            recommendations.append(balance.assessment)

        # 회복 추천
        if recovery.assessment:
            recommendations.append(recovery.assessment)

        # 볼륨 급증 경고
        if vol_change > 20:
            recommendations.append(
                f"⚠️ 전주 대비 볼륨 {vol_change:.0f}%↑ — "
                "급격한 증가는 부상 위험. 다음 주 10% 이내 증가 권장"
            )

        # 운동 다양성
        if len(all_ex_names) <= 3 and wk_count >= 3:
            recommendations.append(
                "💡 운동 종류가 적음 — 다양한 자극을 위해 보조 운동 추가 고려"
            )

        # 체성분 연동
        if body_metrics and len(body_metrics) >= 2:
            bm = sorted(body_metrics, key=lambda m: m.date)
            weight_diff = bm[-1].weight_kg - bm[0].weight_kg
            if weight_diff > 0.5 and vol_change > 0:
                highlights.append("체중↑ + 볼륨↑: 벌크업 진행 중")
            elif weight_diff < -0.5 and vol_change >= 0:
                highlights.append("체중↓ + 볼륨 유지: 효과적 컷팅")
            elif weight_diff < -0.5 and vol_change < -10:
                recommendations.append(
                    "⚠️ 체중↓ + 볼륨↓ — 근손실 위험. 단백질 섭취 확인"
                )

        week_label = ""
        if week_sessions:
            dates = sorted(s.date for s in week_sessions)
            week_label = f"{dates[0]} ~ {dates[-1]}"

        return WeeklyInsight(
            week_label=week_label,
            total_volume=curr_vol,
            prev_week_volume=prev_vol,
            volume_change_pct=vol_change,
            workout_count=wk_count,
            reset_count=rs_count,
            unique_exercises=len(all_ex_names),
            muscle_balance=balance,
            recovery=recovery,
            prs=prs,
            top_exercises=top_exercises,
            highlights=highlights,
            recommendations=recommendations,
        )

    def period_insight(
        self,
        sessions: list[Session],
        body_metrics: list[BodyMetrics],
        period_days: int,
    ) -> PeriodInsight:
        """기간별 종합 인사이트."""

        total_vol = sum(s.total_volume for s in sessions)
        weeks = max(1, period_days / 7)
        avg_weekly_vol = total_vol / weeks
        avg_weekly_sessions = len(sessions) / weeks

        # 운동별 추세
        trends = self._calc_exercise_trends(sessions, period_days)

        plateaus = [t.name for t in trends if t.stagnation_weeks >= 3]
        improving = [t.name for t in trends if t.trend_direction == "상승"]
        declining = [t.name for t in trends if t.trend_direction == "하락"]

        # 일관성 (훈련일 / 기간)
        unique_dates = set(s.date for s in sessions)
        expected_days = period_days * 4 / 7  # assume ~4 days/week training
        consistency = min(100, len(unique_dates) / max(1, expected_days) * 100)

        # 체성분 요약
        body_summary = None
        if body_metrics:
            bm = sorted(body_metrics, key=lambda m: m.date)
            if len(bm) >= 2:
                w_change = bm[-1].weight_kg - bm[0].weight_kg
                sign = "+" if w_change > 0 else ""
                body_summary = f"체중: {bm[0].weight_kg} → {bm[-1].weight_kg}kg ({sign}{w_change:.1f}kg)"
                if bm[-1].body_fat_pct and bm[0].body_fat_pct:
                    bf_change = bm[-1].body_fat_pct - bm[0].body_fat_pct
                    sign_bf = "+" if bf_change > 0 else ""
                    body_summary += f" | 체지방: {sign_bf}{bf_change:.1f}%"

        # 성과
        achievements = []
        if improving:
            achievements.append(f"성장 중인 운동: {', '.join(improving[:3])}")
        all_prs = self._find_all_prs_in_period(sessions)
        if all_prs:
            achievements.append(f"기간 내 신기록: {len(all_prs)}개")
        if avg_weekly_sessions >= 4:
            achievements.append(f"평균 주 {avg_weekly_sessions:.1f}회 — 높은 훈련 빈도")

        # 포커스 영역
        focus = []
        if plateaus:
            focus.append(f"정체기 운동: {', '.join(plateaus[:3])} — 프로그램 변경 권장")
        if declining:
            focus.append(f"약세 운동: {', '.join(declining[:3])} — 볼륨/빈도 점검")
        if consistency < 60:
            focus.append(f"일관성 {consistency:.0f}% — 규칙적 훈련 습관 필요")

        # 실행 항목
        actions = []
        for t in trends:
            if t.stagnation_weeks >= 3:
                actions.append(
                    f"{t.name}: {t.stagnation_weeks}주 정체 → "
                    "중량 2.5kg↑ 또는 세트/렙 구성 변경 시도"
                )
        if avg_weekly_vol > 0 and not actions:
            # 볼륨 추이 기반 제안
            weekly_vols = self._get_weekly_volumes(sessions)
            if len(weekly_vols) >= 3:
                recent_avg = sum(weekly_vols[-2:]) / 2
                older_avg = sum(weekly_vols[:-2]) / max(1, len(weekly_vols) - 2)
                if older_avg > 0:
                    vol_trend_pct = (recent_avg - older_avg) / older_avg * 100
                    if vol_trend_pct > 20:
                        actions.append(
                            f"최근 볼륨 급증 ({vol_trend_pct:.0f}%↑) — "
                            "디로드 주간 계획 고려"
                        )

        return PeriodInsight(
            period_days=period_days,
            total_sessions=len(sessions),
            total_volume=total_vol,
            avg_weekly_volume=avg_weekly_vol,
            avg_weekly_sessions=avg_weekly_sessions,
            exercise_trends=trends,
            plateaus=plateaus,
            improving_exercises=improving,
            declining_exercises=declining,
            body_summary=body_summary,
            consistency_pct=consistency,
            top_achievements=achievements,
            focus_areas=focus,
            action_items=actions,
        )

    # ─── PR 감지 ───

    def _detect_prs(
        self, session: Session, history: list[Session],
    ) -> list[PersonalRecord]:
        """현재 세션에서 달성한 개인 기록 감지."""
        prs = []

        # 운동별 역대 기록 수집
        historical: dict[str, dict] = defaultdict(
            lambda: {"best_1rm": 0, "max_weight": 0, "max_reps": 0,
                     "max_volume": 0, "max_duration": 0}
        )
        for s in history:
            for ex in s.exercises:
                h = historical[ex.name]
                if ex.top_set:
                    h["best_1rm"] = max(h["best_1rm"], ex.top_set.estimated_1rm)
                for st in ex.sets:
                    if st.weight_kg:
                        h["max_weight"] = max(h["max_weight"], st.weight_kg)
                    if st.reps and not st.weight_kg:
                        h["max_reps"] = max(h["max_reps"], st.reps)
                    if st.duration_sec:
                        h["max_duration"] = max(h["max_duration"], st.duration_sec)
                if ex.total_volume > 0:
                    h["max_volume"] = max(h["max_volume"], ex.total_volume)

        # 현재 세션과 비교
        for ex in session.exercises:
            h = historical[ex.name]

            # 1RM PR
            if ex.top_set and ex.top_set.estimated_1rm > 0:
                curr_1rm = ex.top_set.estimated_1rm
                if h["best_1rm"] > 0 and curr_1rm > h["best_1rm"]:
                    improvement = curr_1rm - h["best_1rm"]
                    prs.append(PersonalRecord(
                        exercise_name=ex.name, record_type="1rm",
                        new_value=curr_1rm, previous_best=h["best_1rm"],
                        improvement=improvement,
                        improvement_pct=(improvement / h["best_1rm"] * 100),
                        achieved_date=session.date,
                    ))

            # 최고 중량 PR
            for st in ex.sets:
                if st.weight_kg and h["max_weight"] > 0:
                    if st.weight_kg > h["max_weight"]:
                        prs.append(PersonalRecord(
                            exercise_name=ex.name, record_type="max_weight",
                            new_value=st.weight_kg, previous_best=h["max_weight"],
                            improvement=st.weight_kg - h["max_weight"],
                            improvement_pct=((st.weight_kg - h["max_weight"])
                                             / h["max_weight"] * 100),
                            achieved_date=session.date,
                        ))
                        break  # 1 PR per exercise per type

            # 세션 볼륨 PR
            if ex.total_volume > 0 and h["max_volume"] > 0:
                if ex.total_volume > h["max_volume"]:
                    improvement = ex.total_volume - h["max_volume"]
                    prs.append(PersonalRecord(
                        exercise_name=ex.name, record_type="max_volume",
                        new_value=ex.total_volume, previous_best=h["max_volume"],
                        improvement=improvement,
                        improvement_pct=(improvement / h["max_volume"] * 100),
                        achieved_date=session.date,
                    ))

            # 최장 시간 PR (timed exercises)
            for st in ex.sets:
                if st.duration_sec and h["max_duration"] > 0:
                    if st.duration_sec > h["max_duration"]:
                        prs.append(PersonalRecord(
                            exercise_name=ex.name, record_type="max_duration",
                            new_value=st.duration_sec,
                            previous_best=h["max_duration"],
                            improvement=st.duration_sec - h["max_duration"],
                            improvement_pct=((st.duration_sec - h["max_duration"])
                                             / h["max_duration"] * 100),
                            achieved_date=session.date,
                        ))
                        break

        # 중복 제거 (같은 운동+같은 타입 → 1RM이 max_weight보다 우선)
        seen = set()
        unique_prs = []
        for pr in prs:
            key = (pr.exercise_name, pr.record_type)
            if key not in seen:
                seen.add(key)
                unique_prs.append(pr)

        return unique_prs

    # ─── 볼륨 비교 ───

    def _compare_volumes(
        self, session: Session, history: list[Session],
    ) -> list[VolumeComparison]:
        """각 운동의 볼륨을 이전 동일 운동과 비교."""
        comparisons = []
        for ex in session.exercises:
            if ex.total_volume <= 0:
                continue

            # 같은 운동의 가장 최근 기록 찾기
            prev_volume = 0.0
            prev_date = None
            for s in sorted(history, key=lambda s: s.date, reverse=True):
                for prev_ex in s.exercises:
                    if prev_ex.name == ex.name and prev_ex.total_volume > 0:
                        prev_volume = prev_ex.total_volume
                        prev_date = s.date
                        break
                if prev_date:
                    break

            if prev_volume > 0 and prev_date:
                change = (ex.total_volume - prev_volume) / prev_volume * 100
                days_since = (session.date - prev_date).days
                comparisons.append(VolumeComparison(
                    exercise_name=ex.name,
                    current_volume=ex.total_volume,
                    previous_volume=prev_volume,
                    change_pct=change,
                    days_since_last=days_since,
                ))

        return comparisons

    # ─── 피로도 감지 ───

    def _detect_fatigue(self, session: Session) -> list[FatigueIndicator]:
        """세트 간 퍼포먼스 하락으로 피로도 감지."""
        indicators = []
        for ex in session.exercises:
            weighted_sets = [s for s in ex.sets if s.weight_kg and s.reps]
            if len(weighted_sets) < 2:
                continue

            # 같은 중량 세트에서 렙 변화
            same_weight_groups: dict[float, list[ExerciseSet]] = defaultdict(list)
            for s in weighted_sets:
                same_weight_groups[s.weight_kg].append(s)

            for weight, sets in same_weight_groups.items():
                if len(sets) < 2:
                    continue
                first_reps = sets[0].reps
                last_reps = sets[-1].reps
                if first_reps and last_reps and first_reps > 0:
                    decline = (first_reps - last_reps) / first_reps * 100
                    if decline > 10:  # 10% 이상 하락만
                        if decline > 40:
                            assessment = "과도"
                        elif decline > 25:
                            assessment = "높음"
                        else:
                            assessment = "정상"
                        indicators.append(FatigueIndicator(
                            exercise_name=f"{ex.name} ({weight}kg)",
                            first_set_performance=first_reps,
                            last_set_performance=last_reps,
                            decline_pct=decline,
                            assessment=assessment,
                        ))
                    break  # 가장 큰 그룹만

            # 점감 세트 (중량 하강): 전체 1RM 변화
            if len(weighted_sets) >= 3:
                first_1rm = weighted_sets[0].estimated_1rm
                last_1rm = weighted_sets[-1].estimated_1rm
                if first_1rm > 0:
                    decline = (first_1rm - last_1rm) / first_1rm * 100
                    if decline > 30:
                        indicators.append(FatigueIndicator(
                            exercise_name=ex.name,
                            first_set_performance=first_1rm,
                            last_set_performance=last_1rm,
                            decline_pct=decline,
                            assessment="과도" if decline > 50 else "높음",
                        ))

        return indicators

    # ─── 강도 점수 ───

    def _calc_intensity(
        self, session: Session, history: list[Session],
    ) -> IntensityScore:
        """세션 강도 점수 (0-100)."""
        components: dict[str, float] = {}

        # 1) 볼륨 점수: 최근 동일 타입 세션 대비
        same_type = [
            s for s in history
            if s.session_type == session.session_type
        ]
        if same_type:
            recent_vols = sorted(
                [s.total_volume for s in same_type[-10:]],
            )
            if recent_vols:
                max_vol = max(recent_vols) if recent_vols else 1
                vol_score = min(100, (session.total_volume / max(1, max_vol)) * 80)
            else:
                vol_score = 50
        else:
            vol_score = 50
        components["volume"] = vol_score

        # 2) 상대 강도: 각 운동의 top set이 역대 best 대비 몇 %인지
        intensity_scores = []
        for ex in session.exercises:
            if not ex.top_set:
                continue
            curr_1rm = ex.top_set.estimated_1rm
            best_1rm = 0
            for s in history:
                for h_ex in s.exercises:
                    if h_ex.name == ex.name and h_ex.top_set:
                        best_1rm = max(best_1rm, h_ex.top_set.estimated_1rm)
            # 현재 세션의 best도 포함
            best_1rm = max(best_1rm, curr_1rm)
            if best_1rm > 0:
                intensity_scores.append(curr_1rm / best_1rm * 100)

        if intensity_scores:
            components["relative_intensity"] = sum(intensity_scores) / len(intensity_scores)
        else:
            components["relative_intensity"] = 50

        # 3) 밀도: 세트 수
        set_count = sum(len(ex.sets) for ex in session.exercises)
        density_score = min(100, set_count * 5)  # 20 sets = 100
        components["density"] = density_score

        # 종합
        score = (
            components["volume"] * 0.35
            + components["relative_intensity"] * 0.45
            + components["density"] * 0.20
        )
        score = min(100, max(0, score))

        if score >= 80:
            label = "최대강도"
        elif score >= 60:
            label = "고강도"
        elif score >= 40:
            label = "중강도"
        else:
            label = "저강도"

        return IntensityScore(score=score, components=components, label=label)

    # ─── 근육군 균형 ───

    def _calc_muscle_balance(self, sessions: list[Session]) -> MuscleGroupBalance:
        group_vols: dict[str, float] = defaultdict(float)
        for s in sessions:
            for ex in s.exercises:
                group = classify_muscle_group(ex.name)
                group_vols[group] += ex.total_volume

        total = sum(group_vols.values()) or 1
        push = group_vols.get("push", 0)
        pull = group_vols.get("pull", 0)
        legs = group_vols.get("legs", 0)
        core = group_vols.get("core", 0)
        other = group_vols.get("other", 0)

        push_pct = push / total * 100
        pull_pct = pull / total * 100
        legs_pct = legs / total * 100

        assessment = ""
        if total > 0 and (push + pull + legs) > 0:
            if push > 0 and pull > 0:
                ratio = push / pull
                if ratio > 1.5:
                    assessment = "⚠️ Push > Pull 불균형 — 등/당기기 운동 보강 권장"
                elif ratio < 0.67:
                    assessment = "⚠️ Pull > Push 불균형 — 밀기 운동 보강 권장"
            if legs_pct < 20 and push_pct + pull_pct > 60:
                if assessment:
                    assessment += " | "
                assessment += "🦵 하체 비중 낮음 — 하체 운동 추가 권장"

        return MuscleGroupBalance(
            push_volume=push, pull_volume=pull, legs_volume=legs,
            core_volume=core, other_volume=other,
            push_pct=push_pct, pull_pct=pull_pct, legs_pct=legs_pct,
            assessment=assessment,
        )

    # ─── 회복 패턴 ───

    def _calc_recovery(self, sessions: list[Session]) -> RecoveryPattern:
        if not sessions:
            return RecoveryPattern(
                avg_rest_days=0, min_rest_days=0, max_rest_days=0,
                consecutive_training_days=0, assessment="",
            )

        dates = sorted(set(s.date for s in sessions))
        if len(dates) < 2:
            return RecoveryPattern(
                avg_rest_days=0, min_rest_days=0, max_rest_days=0,
                consecutive_training_days=1, assessment="",
            )

        gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        # gaps에서 0 제거 (같은 날 여러 세션)
        rest_gaps = [g - 1 for g in gaps if g > 0]  # 훈련일 사이 휴식일

        avg_rest = sum(rest_gaps) / len(rest_gaps) if rest_gaps else 0
        min_rest = min(rest_gaps) if rest_gaps else 0
        max_rest = max(rest_gaps) if rest_gaps else 0

        # 연속 훈련일
        max_consecutive = 1
        curr_consecutive = 1
        for i in range(1, len(dates)):
            if (dates[i] - dates[i - 1]).days == 1:
                curr_consecutive += 1
                max_consecutive = max(max_consecutive, curr_consecutive)
            else:
                curr_consecutive = 1

        assessment = ""
        if avg_rest < 0.5:
            assessment = "⚠️ 휴식 부족 — 매일 훈련은 과훈련 위험. 최소 주 1-2일 휴식"
        elif avg_rest > 3:
            assessment = "💤 휴식 과다 — 훈련 빈도 높이면 더 빠른 성장 가능"
        elif max_consecutive >= 4:
            assessment = f"⚡ 최대 {max_consecutive}일 연속 훈련 감지 — 연속 3일 이하 권장"

        return RecoveryPattern(
            avg_rest_days=avg_rest, min_rest_days=min_rest,
            max_rest_days=max_rest,
            consecutive_training_days=max_consecutive,
            assessment=assessment,
        )

    # ─── 운동별 추세 ───

    def _calc_exercise_trends(
        self, sessions: list[Session], period_days: int,
    ) -> list[ExerciseTrend]:
        by_name: dict[str, list[tuple[date, Exercise]]] = defaultdict(list)
        for s in sessions:
            for ex in s.exercises:
                if ex.is_weighted:
                    by_name[ex.name].append((s.date, ex))

        trends = []
        for name, entries in by_name.items():
            entries.sort(key=lambda x: x[0])
            if len(entries) < 2:
                continue

            e1rms = []
            for d, ex in entries:
                if ex.top_set:
                    e1rms.append((d, ex.top_set.estimated_1rm))

            if not e1rms:
                continue

            first_1rm = e1rms[0][1]
            latest_1rm = e1rms[-1][1]
            best_1rm = max(v for _, v in e1rms)

            # 주간 변화율
            total_days = max(1, (e1rms[-1][0] - e1rms[0][0]).days)
            total_change = latest_1rm - first_1rm
            weekly_rate = total_change / (total_days / 7) if total_days > 7 else total_change

            # 정체 감지
            stag = self._detect_stagnation(e1rms)

            # 추세 방향
            if stag >= 3:
                direction = "정체"
            elif total_change > 1:
                direction = "상승"
            elif total_change < -1:
                direction = "하락"
            else:
                direction = "정체"

            total_vol = sum(ex.total_volume for _, ex in entries)
            avg_vol = total_vol / len(entries)

            trends.append(ExerciseTrend(
                name=name, data_points=len(entries),
                first_1rm=first_1rm, latest_1rm=latest_1rm,
                best_1rm=best_1rm, trend_direction=direction,
                weekly_change_rate=weekly_rate,
                stagnation_weeks=stag,
                total_volume=total_vol, avg_session_volume=avg_vol,
            ))

        trends.sort(key=lambda t: t.total_volume, reverse=True)
        return trends

    def _detect_stagnation(self, e1rms: list[tuple[date, float]]) -> int:
        """연속 정체 주 수 계산."""
        if len(e1rms) < 2:
            return 0

        # 주별 최고 1RM
        weekly: dict[date, float] = {}
        for d, val in e1rms:
            wk = d - timedelta(days=d.weekday())
            weekly[wk] = max(weekly.get(wk, 0), val)

        sorted_weeks = sorted(weekly.items(), key=lambda x: x[0], reverse=True)
        if len(sorted_weeks) < 2:
            return 0

        ref = sorted_weeks[0][1]
        stag = 0
        for _, val in sorted_weeks[1:]:
            if abs(val - ref) < 1.0:  # 1kg 미만 차이 = 정체
                stag += 1
            else:
                break
        return stag

    def _get_weekly_volumes(self, sessions: list[Session]) -> list[float]:
        """주별 총 볼륨 리스트."""
        weekly: dict[date, float] = defaultdict(float)
        for s in sessions:
            wk = s.date - timedelta(days=s.date.weekday())
            weekly[wk] += s.total_volume
        return [v for _, v in sorted(weekly.items())]

    def _find_all_prs_in_period(self, sessions: list[Session]) -> list[PersonalRecord]:
        """기간 내 모든 PR 찾기."""
        all_prs = []
        history: list[Session] = []
        for sess in sorted(sessions, key=lambda s: s.date):
            prs = self._detect_prs(sess, history)
            all_prs.extend(prs)
            history.append(sess)
        return all_prs


# ─── 텍스트 렌더링 ───


def render_session_insight(si: SessionInsight) -> str:
    """세션 인사이트를 텍스트로 렌더링."""
    lines = []

    if si.highlights or si.warnings or si.prs:
        lines.append(f"───── 인사이트 ({si.session_label}) ─────")

    if si.prs:
        for pr in si.prs:
            if pr.record_type == "1rm":
                lines.append(
                    f"🏆 {pr.exercise_name} 1RM 신기록! "
                    f"{pr.previous_best:.1f} → {pr.new_value:.1f}kg "
                    f"(+{pr.improvement:.1f}kg, +{pr.improvement_pct:.1f}%)"
                )
            elif pr.record_type == "max_weight":
                lines.append(
                    f"🏋️ {pr.exercise_name} 최고중량 갱신! {pr.new_value:.1f}kg"
                )
            elif pr.record_type == "max_volume":
                lines.append(
                    f"📊 {pr.exercise_name} 볼륨 신기록! "
                    f"{pr.new_value:,.0f}kg (+{pr.improvement_pct:.0f}%)"
                )
            elif pr.record_type == "max_duration":
                lines.append(
                    f"⏱️ {pr.exercise_name} 최장기록! {pr.new_value:.0f}초"
                )

    for vc in si.volume_comparisons:
        sign = "+" if vc.change_pct > 0 else ""
        lines.append(
            f"  {vc.exercise_name}: 볼륨 {sign}{vc.change_pct:.0f}% "
            f"({vc.previous_volume:,.0f} → {vc.current_volume:,.0f}kg, "
            f"{vc.days_since_last}일 전 대비)"
        )

    for w in si.warnings:
        lines.append(w)

    lines.append(
        f"강도: {si.intensity.label} ({si.intensity.score:.0f}/100) | "
        f"운동 {si.exercise_count}종목 {si.set_count}세트"
    )
    if si.total_volume > 0:
        lines.append(f"총 볼륨: {si.total_volume:,.0f}kg")

    return "\n".join(lines)


def render_weekly_insight(wi: WeeklyInsight) -> str:
    """주간 인사이트를 텍스트로 렌더링."""
    lines = [f"═══ 주간 인사이트 ({wi.week_label}) ═══\n"]

    # 요약
    vol_sign = "+" if wi.volume_change_pct > 0 else ""
    lines.append(
        f"오운동 {wi.workout_count}회"
        + (f" + 더리셋 {wi.reset_count}회" if wi.reset_count else "")
        + f" | 볼륨 {wi.total_volume:,.0f}kg ({vol_sign}{wi.volume_change_pct:.0f}%)"
        + f" | {wi.unique_exercises}종목"
    )

    # 하이라이트
    if wi.highlights:
        lines.append("")
        for h in wi.highlights:
            lines.append(h)

    # 근육군 균형
    mb = wi.muscle_balance
    total = mb.push_volume + mb.pull_volume + mb.legs_volume + mb.core_volume + mb.other_volume
    if total > 0:
        lines.append(
            f"\n📊 Push {mb.push_pct:.0f}% | Pull {mb.pull_pct:.0f}% "
            f"| Legs {mb.legs_pct:.0f}%"
        )

    # 상위 운동
    if wi.top_exercises:
        lines.append("\n🔝 이번 주 주요 운동:")
        for name, vol in wi.top_exercises[:5]:
            if vol > 0:
                lines.append(f"  {name}: {vol:,.0f}kg")

    # PR
    if wi.prs:
        lines.append(f"\n🏆 이번 주 신기록 {len(wi.prs)}개:")
        for pr in wi.prs[:5]:
            if pr.record_type == "1rm":
                lines.append(
                    f"  {pr.exercise_name}: "
                    f"{pr.previous_best:.1f} → {pr.new_value:.1f}kg"
                )

    # 추천
    if wi.recommendations:
        lines.append("\n💡 추천:")
        for r in wi.recommendations:
            lines.append(f"  {r}")

    return "\n".join(lines)


def render_period_insight(pi: PeriodInsight) -> str:
    """기간 인사이트를 텍스트로 렌더링."""
    lines = [f"═══ 종합 분석 (최근 {pi.period_days}일) ═══\n"]

    lines.append(
        f"세션 {pi.total_sessions}회 | "
        f"총 볼륨 {pi.total_volume:,.0f}kg | "
        f"주평균 {pi.avg_weekly_volume:,.0f}kg"
    )
    lines.append(
        f"주평균 {pi.avg_weekly_sessions:.1f}회 | "
        f"일관성 {pi.consistency_pct:.0f}%"
    )

    if pi.body_summary:
        lines.append(f"\n🏋️ {pi.body_summary}")

    # 성과
    if pi.top_achievements:
        lines.append("\n🏆 주요 성과:")
        for a in pi.top_achievements:
            lines.append(f"  {a}")

    # 운동별 추세
    if pi.exercise_trends:
        lines.append("\n📈 운동별 추세:")
        for t in pi.exercise_trends[:8]:
            icon = {"상승": "↑", "하락": "↓", "정체": "→"}.get(t.trend_direction, "")
            line = (
                f"  {t.name} {icon} "
                f"1RM: {t.first_1rm:.1f} → {t.latest_1rm:.1f}kg"
            )
            if t.stagnation_weeks >= 3:
                line += f" (⚠ {t.stagnation_weeks}주 정체)"
            lines.append(line)

    # 포커스 영역
    if pi.focus_areas:
        lines.append("\n🎯 포커스:")
        for f in pi.focus_areas:
            lines.append(f"  {f}")

    # 실행 항목
    if pi.action_items:
        lines.append("\n✅ 액션:")
        for a in pi.action_items:
            lines.append(f"  {a}")

    return "\n".join(lines)
