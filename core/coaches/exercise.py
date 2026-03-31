"""운동 코치 비즈니스 로직 — InsightEngine 래핑 + 해석 레이어.

InsightEngine의 수치 분석 결과를 해석하여 코칭 관점의 판단을 제공한다.
Claude API를 호출하지 않는다 — 모든 분석은 코드 기반.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from core.models import Session, SessionType, Exercise, ExerciseSet
from core.insight_engine import (
    InsightEngine,
    SessionInsight,
    WeeklyInsight,
    PeriodInsight,
    ExerciseTrend,
    FatigueIndicator,
    MuscleGroupBalance,
    render_session_insight,
    render_weekly_insight,
    render_period_insight,
    classify_muscle_group,
)


# --- RPE Estimation ---


def estimate_session_rpe(
    session: Session,
    session_insight: SessionInsight,
) -> float:
    """세션 평균 RPE 추정 (피로도 기반).

    Rules:
    - decline <10%: RPE 6-7
    - decline 10-25%: RPE 7-8
    - decline 25-40%: RPE 8-9
    - decline >40%: RPE 9-10

    명시적 RPE가 세트에 있으면 우선 사용.
    """
    # 명시적 RPE 값 확인
    explicit_rpes: list[float] = []
    for ex in session.exercises:
        for s in ex.sets:
            if s.rpe is not None:
                explicit_rpes.append(s.rpe)

    if explicit_rpes:
        return round(sum(explicit_rpes) / len(explicit_rpes), 1)

    # 피로도 지표로 추정
    if not session_insight.fatigue_indicators:
        return 7.0  # default moderate

    rpe_estimates: list[float] = []
    for fi in session_insight.fatigue_indicators:
        decline = fi.decline_pct
        if decline < 10:
            rpe_estimates.append(6.5)
        elif decline < 25:
            rpe_estimates.append(7.5)
        elif decline < 40:
            rpe_estimates.append(8.5)
        else:
            rpe_estimates.append(9.5)

    return round(sum(rpe_estimates) / len(rpe_estimates), 1)


# --- SFR (Stimulus to Fatigue Ratio) ---


def assess_sfr(session_insight: SessionInsight) -> dict:
    """SFR 평가. High SFR = 적절한 자극 대비 관리 가능한 피로."""
    results: list[dict] = []

    for fi in session_insight.fatigue_indicators:
        if fi.assessment == "정상":
            sfr = "high"
            advice = "적절한 자극"
        elif fi.assessment == "높음":
            sfr = "moderate"
            advice = "세트 수 감소 또는 휴식 시간 증가 고려"
        else:  # 과도
            sfr = "low"
            advice = "중량 감소 또는 세트 제거 필요"

        results.append({
            "exercise": fi.exercise_name,
            "decline_pct": round(fi.decline_pct, 1),
            "sfr": sfr,
            "advice": advice,
        })

    overall = "good"
    low_count = sum(1 for r in results if r["sfr"] == "low")
    mod_count = sum(1 for r in results if r["sfr"] == "moderate")
    if low_count >= 2:
        overall = "poor"
    elif low_count >= 1 or mod_count >= 2:
        overall = "moderate"

    return {"exercises": results, "overall_sfr": overall}


# --- Intensity Classification ---


def classify_session_intensity(session_insight: SessionInsight) -> dict:
    """세션 강도 분류 + 주간 배분 가이드.

    IntensityScore 해석:
    - 저강도 (0-39): recovery/technique
    - 중강도 (40-59): standard training
    - 고강도 (60-79): strong stimulus
    - 최대강도 (80-100): PR/test
    """
    score = session_insight.intensity.score
    label = session_insight.intensity.label

    guidance = {
        "저강도": "회복 세션 또는 기술 연습. 더리셋에 적합",
        "중강도": "표준 훈련. 주중 대부분 여기",
        "고강도": "강한 자극. 주 1-2회 적절",
        "최대강도": "테스트 또는 PR 도전. 주 최대 1회, 이후 48시간+ 회복",
    }

    return {
        "score": round(score, 1),
        "label": label,
        "guidance": guidance.get(label, ""),
    }


# --- Deload Detection ---


def check_deload_needed(
    sessions: list[Session],
    period_insight: Optional[PeriodInsight] = None,
    session_insights: list[SessionInsight] | None = None,
) -> dict:
    """디로드 필요 여부 판단.

    Triggers:
    1. 전 종목 3주+ 정체 (PeriodInsight의 ExerciseTrend 기준)
    2. 연속 2세션 피로 '과도' 감지
    """
    reasons: list[str] = []

    # 1. 정체 체크 — ExerciseTrend의 stagnation_weeks 활용
    if period_insight and period_insight.exercise_trends:
        stagnant_count = sum(
            1 for et in period_insight.exercise_trends
            if et.stagnation_weeks >= 3
        )
        total_exercises = len(period_insight.exercise_trends)
        if total_exercises > 0 and stagnant_count == total_exercises:
            reasons.append(f"전 종목 3주+ 정체 ({stagnant_count}개)")

    # 2. 연속 피로 체크
    if session_insights and len(session_insights) >= 2:
        recent_two = session_insights[-2:]
        excessive_count = 0
        for si in recent_two:
            has_excessive = any(
                fi.assessment == "과도" for fi in si.fatigue_indicators
            )
            if has_excessive:
                excessive_count += 1
        if excessive_count >= 2:
            reasons.append("연속 2세션 피로 '과도' 감지")

    return {
        "deload_recommended": len(reasons) > 0,
        "reasons": reasons,
        "prescription": (
            "1주간 볼륨 40-50% 감소, 강도 유지 또는 소폭 감소"
            if reasons else ""
        ),
    }


# --- Movement Quality (더리셋 Integration) ---


def assess_reset_integration(sessions: list[Session], days: int = 14) -> dict:
    """더리셋 통합 평가.

    Targets:
    - Frequency: 주 1-2회
    - 더리셋/오운동 ratio: 최소 1:3
    - 2주+ 더리셋 없음 → 경고
    """
    cutoff = date.today() - timedelta(days=days)
    recent = [s for s in sessions if s.date >= cutoff]

    workout_count = sum(1 for s in recent if s.session_type == SessionType.WORKOUT)
    reset_count = sum(1 for s in recent if s.session_type == SessionType.RESET)
    weeks = max(1, days / 7)

    weekly_reset = reset_count / weeks
    ratio = f"{reset_count}:{workout_count}" if workout_count > 0 else "N/A"

    warnings: list[str] = []
    if reset_count == 0 and days >= 14:
        warnings.append(f"{days}일간 더리셋 0회 — 움직임 품질 점검 필요")
    elif weekly_reset < 1 and workout_count >= 3:
        warnings.append(f"주간 더리셋 {weekly_reset:.1f}회 — 최소 주 1회 권장")

    if workout_count > 0 and reset_count > 0:
        actual_ratio = workout_count / reset_count
        if actual_ratio > 3:
            warnings.append(
                f"더리셋/오운동 비율 1:{actual_ratio:.0f} — 최소 1:3 권장"
            )

    return {
        "reset_count": reset_count,
        "workout_count": workout_count,
        "weekly_reset_avg": round(weekly_reset, 1),
        "ratio": ratio,
        "warnings": warnings,
    }


# --- Muscle Balance ---


def assess_muscle_balance(weekly_insight: WeeklyInsight) -> dict:
    """근육군 균형 평가 (Push/Pull/Legs).

    Push/Pull 비율 1.5 초과 또는 0.67 미만 시 경고.
    """
    balance: MuscleGroupBalance = weekly_insight.muscle_balance
    if not balance:
        return {"status": "insufficient_data"}

    push_vol = balance.push_volume
    pull_vol = balance.pull_volume
    legs_vol = balance.legs_volume

    warnings: list[str] = []

    if push_vol > 0 and pull_vol > 0:
        ratio = push_vol / pull_vol
        if ratio > 1.5:
            warnings.append(
                f"Push/Pull 비율 {ratio:.1f} — Pull 볼륨 증가 필요"
            )
        elif ratio < 0.67:
            warnings.append(
                f"Push/Pull 비율 {ratio:.1f} — Push 볼륨 증가 필요"
            )

    return {
        "push_volume": push_vol,
        "pull_volume": pull_vol,
        "legs_volume": legs_vol,
        "core_volume": balance.core_volume,
        "other_volume": balance.other_volume,
        "assessment": balance.assessment,
        "warnings": warnings,
    }


# --- Inter-Coach Data Contract ---


def build_exercise_to_recovery_payload(
    session: Session,
    session_insight: SessionInsight,
) -> dict:
    """Exercise Coach -> Recovery Coach 데이터 계약."""
    muscle_groups = list(set(
        classify_muscle_group(ex.name)
        for ex in session.exercises
        if classify_muscle_group(ex.name) != "other"
    ))

    fatigue_assessments = [
        {
            "exercise": fi.exercise_name,
            "decline_pct": round(fi.decline_pct, 1),
            "assessment": fi.assessment,
        }
        for fi in session_insight.fatigue_indicators
    ]

    return {
        "session_intensity_score": round(session_insight.intensity.score, 1),
        "session_intensity_label": session_insight.intensity.label,
        "total_volume_kg": round(session.total_volume, 1),
        "muscle_groups_worked": muscle_groups,
        "fatigue_assessments": fatigue_assessments,
        "estimated_session_rpe": estimate_session_rpe(session, session_insight),
        "prs_achieved": len(session_insight.prs),
        "session_type": session.session_type.value,
    }


def build_exercise_to_nutrition_payload(
    session: Session,
    session_insight: SessionInsight,
) -> dict:
    """Exercise Coach -> Nutrition Coach 데이터 계약."""
    score = session_insight.intensity.score
    if score >= 80:
        training_type = "heavy_compound"
        base_kcal = 350
    elif score >= 60:
        training_type = "moderate"
        base_kcal = 250
    elif score >= 40:
        training_type = "light_accessory"
        base_kcal = 180
    elif session.session_type == SessionType.RESET:
        training_type = "reset"
        base_kcal = 150
    else:
        training_type = "light_accessory"
        base_kcal = 180

    estimated_expenditure = base_kcal + (session.total_volume * 0.04)

    primary_muscles = list(set(
        classify_muscle_group(ex.name)
        for ex in session.exercises
        if classify_muscle_group(ex.name) != "other"
    ))

    return {
        "training_day_type": training_type,
        "primary_muscle_groups": primary_muscles,
        "session_intensity_score": round(session_insight.intensity.score, 1),
        "total_volume_kg": round(session.total_volume, 1),
        "estimated_calorie_expenditure": round(estimated_expenditure, 0),
        "is_pr_day": len(session_insight.prs) > 0,
    }


# --- Comprehensive Exercise Analysis ---


def analyze_session(session: Session, all_history: list[Session]) -> dict:
    """세션 종합 분석 — Exercise Coach 관점.

    InsightEngine으로 세션 인사이트를 생성한 뒤,
    RPE/SFR/강도/더리셋 통합 등 코칭 해석을 추가한다.
    코치 간 데이터 계약(recovery/nutrition payload)도 함께 생성.
    """
    engine = InsightEngine()
    si = engine.session_insight(session, all_history)

    intensity = classify_session_intensity(si)
    rpe = estimate_session_rpe(session, si)
    sfr = assess_sfr(si)
    reset_info = assess_reset_integration(all_history, days=14)

    rendered = render_session_insight(si)

    return {
        "session_insight": si,
        "rendered": rendered,
        "intensity": intensity,
        "estimated_rpe": rpe,
        "sfr": sfr,
        "reset_integration": reset_info,
        "recovery_payload": build_exercise_to_recovery_payload(session, si),
        "nutrition_payload": build_exercise_to_nutrition_payload(session, si),
    }
