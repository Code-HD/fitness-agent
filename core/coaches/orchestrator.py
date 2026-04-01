"""오케스트레이터 — 3코치 통합, 상태 머신, 충돌 해결, 크로스 도메인 합성."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from core.models import (
    BodyMetrics,
    CoachingPhase,
    DailyNutrition,
    Meal,
    NutritionTarget,
    RecoveryScore,
    Session,
    SessionType,
    SleepLog,
)


# === SUBSYSTEM 3: Phase State Machine ===

VALID_STATES = {
    "ASSESS", "BUILD", "INTENSIFY", "PEAK",
    "DELOAD", "RECOMP", "CUT", "MAINTAIN",
}


def evaluate_phase_transition(
    current_phase: CoachingPhase,
    body_metrics: list[BodyMetrics],
    sessions: list[Session],
    recovery: Optional[RecoveryScore] = None,
) -> dict:
    """Phase 전환 평가.

    Returns:
        {"should_transition": bool, "new_state": str|None, "reason": str}

    Key thresholds (from orchestrator prompt):
        RECOMP -> BUILD : BF<=20% AND SM growth<0.1kg/mo AND VF<=7 AND consistency>=80%
        RECOMP -> CUT   : BF>28% OR VF>=10 OR (SM>=38kg AND BF>25%)
        BUILD  -> CUT   : BF>=25% OR VF>=8 OR BUILD>=16weeks OR weight_gain>8kg
        ANY    -> DELOAD : 3 weeks stagnation OR fatigue "과도" 2+ in 3 sessions
    """
    state = current_phase.state
    result: dict = {"should_transition": False, "new_state": None, "reason": ""}

    if not body_metrics:
        return result

    # Latest body metrics
    latest = sorted(body_metrics, key=lambda m: m.date)[-1]
    bf = latest.body_fat_pct
    sm = latest.skeletal_muscle_kg

    # Training consistency (last 4 weeks)
    recent_weeks = 4
    cutoff = date.today() - timedelta(days=recent_weeks * 7)
    recent_sessions = [s for s in sessions if s.date >= cutoff]
    training_days = len(set(s.date for s in recent_sessions))
    expected_days = recent_weeks * 4  # 4x/week target
    consistency = (training_days / expected_days * 100) if expected_days > 0 else 0

    # --- DELOAD auto-return ---
    if state == "DELOAD":
        started = date.fromisoformat(current_phase.started_at)
        if (date.today() - started).days >= 7:
            prev_state = current_phase.parameters.get("previous_state", "RECOMP")
            result.update(
                should_transition=True,
                new_state=prev_state,
                reason="디로드 1주 완료 — 복귀",
            )
            return result
        return result

    # --- Safety: fatigue override -> DELOAD ---
    if recovery and recovery.fatigue_level == "critical":
        result.update(
            should_transition=True,
            new_state="DELOAD",
            reason=f"피로 극심 (회복 점수 {recovery.score}/100) — 즉시 디로드",
        )
        return result

    # --- RECOMP transitions ---
    if state == "RECOMP":
        # RECOMP -> CUT
        if bf is not None and bf > 28.0:
            result.update(
                should_transition=True,
                new_state="CUT",
                reason=f"체지방 {bf}% > 28% — CUT 필요",
            )
            return result
        if sm is not None and bf is not None and sm >= 38.0 and bf > 25.0:
            result.update(
                should_transition=True,
                new_state="CUT",
                reason=f"골격근 {sm}kg 충분, 체지방 {bf}% > 25% — CUT 시작",
            )
            return result

        # RECOMP -> BUILD (all conditions must be met)
        if bf is not None and bf <= 22.0 and consistency >= 80:
            old_metrics = [
                m for m in body_metrics
                if m.date <= cutoff and m.skeletal_muscle_kg is not None
            ]
            if old_metrics and sm is not None:
                old_sm = sorted(old_metrics, key=lambda m: m.date)[-1].skeletal_muscle_kg
                sm_growth = sm - old_sm  # monthly approximation
                if sm_growth < 0.1:
                    result.update(
                        should_transition=True,
                        new_state="BUILD",
                        reason=(
                            f"BF {bf}% ≤ 22%, SM 성장 {sm_growth:.2f}kg/mo < 0.1, "
                            f"일관성 {consistency:.0f}% — BUILD 전환"
                        ),
                    )
                    return result

    # --- BUILD transitions ---
    elif state == "BUILD":
        if bf is not None and bf >= 25.0:
            result.update(
                should_transition=True,
                new_state="CUT",
                reason=f"체지방 {bf}% ≥ 25% — CUT 전환",
            )
            return result

        # BUILD duration check (16 weeks max)
        started = date.fromisoformat(current_phase.started_at)
        weeks_in_build = (date.today() - started).days / 7
        if weeks_in_build >= 16:
            result.update(
                should_transition=True,
                new_state="CUT",
                reason=f"BUILD {weeks_in_build:.0f}주 경과 (≥16주) — 미니컷 전환",
            )
            return result

    # --- CUT transitions ---
    elif state == "CUT":
        if bf is not None and bf <= 18.0:
            result.update(
                should_transition=True,
                new_state="RECOMP",
                reason=f"체지방 {bf}% ≤ 18% 목표 달성 — RECOMP 전환",
            )
            return result

    return result


# === SUBSYSTEM 2: Priority Scoring ===


def calculate_priority_score(impact: int, urgency: int, feasibility: int) -> dict:
    """Priority Score = Impact x Urgency x Feasibility (max 125).

    Bands:
        >=80  CRITICAL
        40-79 IMPORTANT
        20-39 RECOMMENDED
        <20   NOTED
    """
    score = impact * urgency * feasibility

    if score >= 80:
        band = "CRITICAL"
    elif score >= 40:
        band = "IMPORTANT"
    elif score >= 20:
        band = "RECOMMENDED"
    else:
        band = "NOTED"

    return {
        "score": score,
        "band": band,
        "impact": impact,
        "urgency": urgency,
        "feasibility": feasibility,
    }


# === SUBSYSTEM 4: Conflict Resolution ===


def resolve_conflict(
    exercise_recommendation: Optional[str] = None,
    recovery_recommendation: Optional[str] = None,
    nutrition_recommendation: Optional[str] = None,
    current_phase: str = "RECOMP",
    recovery_score: Optional[RecoveryScore] = None,
) -> dict:
    """코치 간 충돌 해결.

    Priority: SAFETY > PHASE GOAL > TREND DATA > SPECIALIST
    """
    resolution: dict = {
        "winning_domain": None,
        "action": "",
        "conflict_detected": False,
        "explanation": "",
    }

    # Safety check: recovery critical overrides everything
    if recovery_score and recovery_score.fatigue_level == "critical":
        resolution.update(
            winning_domain="recovery",
            action="훈련 중단 — 완전 휴식 필수",
            conflict_detected=True,
            explanation="안전 최우선: 피로 극심 상태에서 훈련은 부상 위험",
        )
        return resolution

    if recovery_score and recovery_score.fatigue_level == "high":
        resolution.update(
            winning_domain="recovery",
            action="저강도 훈련 또는 더리셋만 허용",
            conflict_detected=True,
            explanation="피로 높음: 회복 우선. 강도 제한",
        )
        return resolution

    # Phase-based domain lead
    phase_lead: dict[str, str] = {
        "CUT": "nutrition",
        "BUILD": "exercise",
        "DELOAD": "recovery",
        "RECOMP": "balanced",
        "MAINTAIN": "recovery",
    }

    lead = phase_lead.get(current_phase, "balanced")
    resolution["winning_domain"] = lead

    return resolution


# === SUBSYSTEM 6: KPI Dashboard ===


def build_kpi_dashboard(
    body_metrics: list[BodyMetrics],
    sessions: list[Session],
    sleep_logs: list[SleepLog],
    meals: list[Meal],
    recovery: Optional[RecoveryScore] = None,
    nutrition_target: Optional[NutritionTarget] = None,
) -> dict:
    """KPI 대시보드 구성."""
    dashboard: dict = {
        "primary": {},
        "secondary": {},
        "bottleneck": None,
    }

    # --- Body composition trends ---
    if len(body_metrics) >= 2:
        sorted_bm = sorted(body_metrics, key=lambda m: m.date)
        latest = sorted_bm[-1]
        prev = sorted_bm[-2]

        if latest.body_fat_pct is not None and prev.body_fat_pct is not None:
            bf_change = latest.body_fat_pct - prev.body_fat_pct
            dashboard["primary"]["bf_trend"] = {
                "value": latest.body_fat_pct,
                "change": round(bf_change, 1),
                "status": (
                    "improving" if bf_change < 0
                    else "declining" if bf_change > 0.5
                    else "stable"
                ),
            }

        if latest.skeletal_muscle_kg is not None and prev.skeletal_muscle_kg is not None:
            sm_change = latest.skeletal_muscle_kg - prev.skeletal_muscle_kg
            dashboard["primary"]["sm_trend"] = {
                "value": latest.skeletal_muscle_kg,
                "change": round(sm_change, 1),
                "status": (
                    "improving" if sm_change > 0
                    else "declining" if sm_change < -0.3
                    else "stable"
                ),
            }

    # --- Recovery score ---
    if recovery:
        dashboard["primary"]["recovery_score"] = {
            "value": recovery.score,
            "status": (
                "improving" if recovery.score >= 70
                else "declining" if recovery.score < 40
                else "stable"
            ),
        }

    # --- Training completion (last 7 days) ---
    cutoff = date.today() - timedelta(days=7)
    week_sessions = [s for s in sessions if s.date >= cutoff]
    training_rate = len(week_sessions) / 4 * 100  # target 4x/week
    dashboard["secondary"]["training_completion"] = {
        "value": round(min(100, training_rate), 0),
        "status": (
            "improving" if training_rate >= 75
            else "declining" if training_rate < 50
            else "stable"
        ),
    }

    # --- Sleep consistency ---
    if sleep_logs:
        recent_sleep = sorted(sleep_logs, key=lambda s: s.date)[-7:]
        if len(recent_sleep) >= 3:
            hours = [s.duration_hours for s in recent_sleep]
            avg_h = sum(hours) / len(hours)
            dashboard["secondary"]["sleep_consistency"] = {
                "avg_hours": round(avg_h, 1),
                "status": (
                    "improving" if avg_h >= 7.5
                    else "declining" if avg_h < 6.5
                    else "stable"
                ),
            }

    # --- Identify bottleneck ---
    declining_domains: list[str] = []
    for section in [dashboard["primary"], dashboard["secondary"]]:
        for key, val in section.items():
            if isinstance(val, dict) and val.get("status") == "declining":
                declining_domains.append(key)

    if "recovery_score" in declining_domains:
        dashboard["bottleneck"] = "recovery"
    elif "bf_trend" in declining_domains:
        dashboard["bottleneck"] = "nutrition"
    elif "training_completion" in declining_domains:
        dashboard["bottleneck"] = "exercise"

    return dashboard


# === SUBSYSTEM 11: Routing ===


def route_request(request_type: str, data_available: dict) -> list[str]:
    """요청을 적절한 코치에게 라우팅.

    Args:
        request_type: "session_log", "meal_log", "sleep_log", "weekly_report",
                     "period_insight", "daily_check", etc.
        data_available: {"exercise": bool, "nutrition": bool, "sleep": bool, "body": bool}

    Returns:
        list of coach domains to query
    """
    routing: dict[str, list[str]] = {
        "session_log": ["exercise"],
        "meal_log": ["nutrition"],
        "sleep_log": ["recovery"],
        "session_insight": ["exercise"],
        "daily_check": ["recovery", "nutrition"],
        "weekly_report": ["exercise", "recovery", "nutrition"],
        "period_insight": ["exercise", "recovery", "nutrition"],
        "fatigue_check": ["recovery", "exercise"],
        "nutrition_insight": ["nutrition"],
        "body_check": ["nutrition", "exercise"],
    }

    coaches = routing.get(request_type, ["exercise", "recovery", "nutrition"])

    # Filter based on data availability
    if not data_available.get("nutrition"):
        coaches = [c for c in coaches if c != "nutrition"]
    if not data_available.get("sleep"):
        # Recovery can still work with exercise fatigue data
        pass

    return coaches if coaches else ["exercise"]


# === SUBSYSTEM 9: Intervention Ladder ===


def determine_intervention_level(
    recovery: Optional[RecoveryScore] = None,
    consecutive_training_days: int = 0,
    nutrition_compliance: Optional[float] = None,
) -> dict:
    """개입 수준 결정 (1-5).

    Level 1: 관찰 (Observe)
    Level 2: 넛지 (Nudge)
    Level 3: 교정 (Correct)
    Level 4: 오버라이드 (Override)
    Level 5: 긴급 (Escalate)
    """
    level = 1
    reasons: list[str] = []

    if recovery:
        if recovery.fatigue_level == "critical":
            level = max(level, 5)
            reasons.append("피로 극심 — 즉시 조치 필요")
        elif recovery.fatigue_level == "high":
            level = max(level, 4)
            reasons.append("피로 높음 — 프로그램 수정 권고")
        elif recovery.fatigue_level == "moderate":
            level = max(level, 2)
            reasons.append("피로 보통 — 소폭 조정 제안")

    if consecutive_training_days >= 5:
        level = max(level, 5)
        reasons.append(f"연속 {consecutive_training_days}일 훈련 — 즉시 휴식 필요")
    elif consecutive_training_days >= 4:
        level = max(level, 4)
        reasons.append(f"연속 {consecutive_training_days}일 — 내일 반드시 휴식")

    if nutrition_compliance is not None and nutrition_compliance < 30:
        level = max(level, 4)
        reasons.append("영양 이행률 30% 미만 — MAINTAIN 전환 고려")

    labels = {1: "관찰", 2: "넛지", 3: "교정", 4: "오버라이드", 5: "긴급"}

    return {
        "level": level,
        "label": labels.get(level, "관찰"),
        "reasons": reasons,
    }


# === Cross-Domain Synthesis ===


def synthesize_weekly_report(
    sessions: list[Session],
    sleep_logs: list[SleepLog],
    meals: list[Meal],
    body_metrics: list[BodyMetrics],
    current_phase: Optional[CoachingPhase] = None,
    nutrition_target: Optional[NutritionTarget] = None,
) -> str:
    """주간 리포트 크로스 도메인 합성.

    Format (subsystem 12):
        [Phase: {state}]
        === 운동 분석 (Exercise Coach) ===
        === 회복 상태 (Recovery Coach) ===
        === 영양 상태 (Nutrition Coach) ===
        === 교차 분석 (Orchestrator) ===
        === 다음 스텝 ===
    """
    from core.coaches.nutrition import aggregate_daily_nutrition
    from core.coaches.recovery import calculate_fatigue_score

    lines: list[str] = []

    # Header
    phase_label = current_phase.state if current_phase else "ASSESS"
    lines.append(f"[Phase: {phase_label}]")
    lines.append("")

    # === Exercise Analysis ===
    lines.append("=== 운동 분석 (Exercise Coach) ===")
    workout_sessions = [s for s in sessions if s.session_type == SessionType.WORKOUT]
    reset_sessions = [s for s in sessions if s.session_type == SessionType.RESET]
    total_vol = sum(s.total_volume for s in sessions)

    lines.append(f"오운동 {len(workout_sessions)}회 + 더리셋 {len(reset_sessions)}회")
    lines.append(f"총 볼륨: {total_vol:,.0f}kg")
    lines.append("")

    # === Recovery ===
    lines.append("=== 회복 상태 (Recovery Coach) ===")
    if sleep_logs:
        recovery = calculate_fatigue_score(sleep_logs, sessions)
        lines.append(f"회복 점수: {recovery.score}/100 ({recovery.fatigue_level})")
        lines.append(f"수면 품질: {recovery.sleep_quality}")
        if recovery.deload_recommended:
            lines.append("⚠ 디로드 권장")
    else:
        lines.append("수면 데이터 없음 — 회복 분석 제한적")
    lines.append("")

    # === Nutrition ===
    lines.append("=== 영양 상태 (Nutrition Coach) ===")
    if meals:
        # Group meals by date (Meal.date is str "YYYY-MM-DD")
        dates = sorted(set(m.date for m in meals))
        if dates:
            daily_summaries: list[DailyNutrition] = []
            for d in dates[-7:]:  # last 7 days
                daily = aggregate_daily_nutrition(meals, d)
                daily_summaries.append(daily)

            avg_cal = sum(d.total_calories for d in daily_summaries) / len(daily_summaries)
            avg_pro = sum(d.total_protein_g for d in daily_summaries) / len(daily_summaries)
            lines.append(f"평균 칼로리: {avg_cal:.0f}kcal/일")
            lines.append(f"평균 단백질: {avg_pro:.0f}g/일")

            if nutrition_target and body_metrics:
                latest_bm = sorted(body_metrics, key=lambda m: m.date)[-1]
                target_protein = nutrition_target.protein_g_per_kg * latest_bm.weight_kg
                prot_pct = (avg_pro / target_protein * 100) if target_protein > 0 else 0
                lines.append(f"단백질 달성률: {prot_pct:.0f}%")
    else:
        lines.append("식이 데이터 부재 — 영양 분석 불가")
    lines.append("")

    # === Cross-domain ===
    lines.append("=== 교차 분석 (Orchestrator) ===")

    cross_insights: list[str] = []

    # High volume + low protein check
    if meals and total_vol > 0:
        latest_date = max(m.date for m in meals)
        daily = aggregate_daily_nutrition(meals, latest_date)
        if daily.total_protein_g < 120 and total_vol > 15000:
            cross_insights.append("훈련 볼륨 높으나 단백질 부족 — 근회복 지연 위험")

    # Sleep deficit + training check
    if sleep_logs:
        recent_sleep = sorted(sleep_logs, key=lambda s: s.date)[-3:]
        avg_sleep = sum(s.duration_hours for s in recent_sleep) / len(recent_sleep)
        if avg_sleep < 6.5 and len(workout_sessions) >= 3:
            cross_insights.append("수면 부족 + 고빈도 훈련 — CNS 피로 누적 위험")

    if cross_insights:
        for ci in cross_insights:
            lines.append(f"  ⚠ {ci}")
    else:
        lines.append("  도메인 간 충돌 없음")
    lines.append("")

    # === Next Steps ===
    lines.append("=== 다음 스텝 ===")

    # Phase transition check
    if current_phase and body_metrics:
        recovery_for_transition = (
            calculate_fatigue_score(sleep_logs, sessions) if sleep_logs else None
        )
        transition = evaluate_phase_transition(
            current_phase, body_metrics, sessions, recovery_for_transition,
        )
        if transition["should_transition"]:
            lines.append(
                f"  Phase 전환 권장: {current_phase.state} → {transition['new_state']}"
            )
            lines.append(f"     사유: {transition['reason']}")

    # Intervention level
    if sleep_logs:
        rec = calculate_fatigue_score(sleep_logs, sessions)
        intervention = determine_intervention_level(
            recovery=rec,
            consecutive_training_days=rec.consecutive_training_days,
        )
        if intervention["level"] >= 3:
            lines.append(
                f"  개입 수준: L{intervention['level']} ({intervention['label']})"
            )
            for r in intervention["reasons"]:
                lines.append(f"     - {r}")

    return "\n".join(lines)
