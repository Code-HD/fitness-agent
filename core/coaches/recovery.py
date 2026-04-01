"""회복 코치 비즈니스 로직 — 피로 점수, 수면 분석, 회복 준비도, CNS 피로 추정."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from core.models import RecoveryScore, Session, SessionType, SleepLog


# --- 피로 점수 계산 ---


def calculate_fatigue_score(
    sleep_logs: list[SleepLog],
    recent_sessions: list[Session],
    target_date: str | None = None,
) -> RecoveryScore:
    """피로 점수 산출 (0-100). 높을수록 피로가 심함.

    Components:
    - sleep_debt (0-30): 최근 3-5일 수면 부채
    - training_load (0-30): 최근 세션 강도/볼륨
    - consecutive_days (0-20): 연속 훈련일 수
    - subjective_fatigue (0-10): 수면 품질 기반 추정
    - recovery_violation (0-10): 같은 근육군 48시간 미만 재훈련
    """
    d = target_date or date.today().isoformat()

    # 1. Sleep Debt (0-30)
    sleep_score = _calc_sleep_debt_score(sleep_logs, d)

    # 2. Training Load (0-30)
    training_score = _calc_training_load_score(recent_sessions, d)

    # 3. Consecutive Training Days (0-20)
    consec_days, consec_score = _calc_consecutive_days_score(recent_sessions, d)

    # 4. Subjective Fatigue via Sleep Quality (0-10)
    subjective_score = _calc_subjective_score(sleep_logs, d)

    # 5. Recovery Window Violation (0-10)
    violation_score = _calc_recovery_violation_score(recent_sessions, d)

    total_fatigue = min(100, sleep_score + training_score + consec_score + subjective_score + violation_score)

    # Recovery score = inverse of fatigue
    recovery = max(0, 100 - total_fatigue)

    # Fatigue level classification
    if total_fatigue >= 75:
        fatigue_level = "critical"
    elif total_fatigue >= 55:
        fatigue_level = "high"
    elif total_fatigue >= 30:
        fatigue_level = "moderate"
    else:
        fatigue_level = "low"

    # Sleep quality summary
    sleep_quality = _assess_sleep_quality(sleep_logs, d)

    # Deload recommendation
    deload = fatigue_level in ("critical", "high") and consec_days >= 3

    return RecoveryScore(
        date=d,
        score=recovery,
        fatigue_level=fatigue_level,
        sleep_quality=sleep_quality,
        consecutive_training_days=consec_days,
        deload_recommended=deload,
        details={
            "sleep_debt_score": sleep_score,
            "training_load_score": training_score,
            "consecutive_days_score": consec_score,
            "subjective_score": subjective_score,
            "violation_score": violation_score,
            "total_fatigue": total_fatigue,
        },
    )


# --- Component Calculators ---


def _calc_sleep_debt_score(sleep_logs: list[SleepLog], target_date: str) -> int:
    """수면 부채 점수 (0-30). 최근 5일 평균 기준."""
    target = date.fromisoformat(target_date)
    recent = [
        s for s in sleep_logs
        if target - timedelta(days=5) <= date.fromisoformat(s.date) <= target
    ]

    if not recent:
        return 15  # 데이터 없으면 중간값

    avg_hours = sum(s.duration_hours for s in recent) / len(recent)
    target_hours = 7.5  # 목표 수면 시간

    if avg_hours >= target_hours:
        return 0
    elif avg_hours >= 7.0:
        return 5
    elif avg_hours >= 6.5:
        return 10
    elif avg_hours >= 6.0:
        return 20
    else:
        return 30  # 6시간 미만 = 최대 피로


def _calc_training_load_score(sessions: list[Session], target_date: str) -> int:
    """훈련 부하 점수 (0-30). 최근 3일 세션 기반."""
    target = date.fromisoformat(target_date)
    recent = [
        s for s in sessions
        if target - timedelta(days=3) <= s.date <= target
        and s.session_type == SessionType.WORKOUT
    ]

    if not recent:
        return 0

    total_volume = sum(s.total_volume for s in recent)
    session_count = len(recent)

    # 볼륨 기반 피로 (10,000kg 단위)
    vol_score = min(15, int(total_volume / 10000) * 5)

    # 세션 수 기반 피로
    sess_score = min(15, session_count * 5)

    return min(30, vol_score + sess_score)


def _calc_consecutive_days_score(sessions: list[Session], target_date: str) -> tuple[int, int]:
    """연속 훈련일 수 + 점수 (0-20)."""
    target = date.fromisoformat(target_date)

    # 최근 7일 훈련 날짜 수집
    training_dates = set()
    for s in sessions:
        if target - timedelta(days=7) <= s.date <= target:
            # 더리셋은 피로 가중치 0.3
            training_dates.add(s.date)

    if not training_dates:
        return 0, 0

    # 연속 일수 계산 (target_date부터 역순)
    consecutive = 0
    check_date = target
    while check_date in training_dates:
        consecutive += 1
        check_date -= timedelta(days=1)

    if consecutive >= 5:
        score = 20
    elif consecutive >= 4:
        score = 15
    elif consecutive >= 3:
        score = 10
    elif consecutive >= 2:
        score = 5
    else:
        score = 0

    return consecutive, score


def _calc_subjective_score(sleep_logs: list[SleepLog], target_date: str) -> int:
    """주관적 피로 점수 (0-10). 수면 품질 기반."""
    target = date.fromisoformat(target_date)
    recent = [
        s for s in sleep_logs
        if target - timedelta(days=3) <= date.fromisoformat(s.date) <= target
        and s.quality is not None
    ]

    if not recent:
        return 5  # 데이터 없으면 중간값

    avg_quality = sum(s.quality for s in recent) / len(recent)

    if avg_quality >= 4:
        return 0
    elif avg_quality >= 3:
        return 3
    elif avg_quality >= 2:
        return 7
    else:
        return 10


def _calc_recovery_violation_score(sessions: list[Session], target_date: str) -> int:
    """회복 윈도우 위반 점수 (0-10). 같은 근육군 48시간 미만 재훈련."""
    from core.insight_engine import classify_muscle_group

    target = date.fromisoformat(target_date)
    recent = [
        s for s in sessions
        if target - timedelta(days=3) <= s.date <= target
    ]

    if len(recent) < 2:
        return 0

    # 근육군별 마지막 훈련 날짜 추적
    muscle_dates: dict[str, list[date]] = {}
    for s in sorted(recent, key=lambda x: x.date):
        for ex in s.exercises:
            group = classify_muscle_group(ex.name)
            if group not in muscle_dates:
                muscle_dates[group] = []
            muscle_dates[group].append(s.date)

    violations = 0
    for group, dates in muscle_dates.items():
        if group == "other":
            continue
        for i in range(1, len(dates)):
            gap_hours = (dates[i] - dates[i - 1]).total_seconds() / 3600
            if gap_hours < 48:
                violations += 1

    return min(10, violations * 5)


# --- 수면 품질 평가 ---


def _assess_sleep_quality(sleep_logs: list[SleepLog], target_date: str) -> str:
    """최근 수면 품질 종합 평가."""
    target = date.fromisoformat(target_date)
    recent = [
        s for s in sleep_logs
        if target - timedelta(days=7) <= date.fromisoformat(s.date) <= target
    ]

    if not recent:
        return "unknown"

    avg_hours = sum(s.duration_hours for s in recent) / len(recent)

    # 수면 일관성 체크
    hours_list = [s.duration_hours for s in recent]
    if len(hours_list) >= 2:
        consistency = max(hours_list) - min(hours_list)
    else:
        consistency = 0

    if avg_hours >= 7.5 and consistency < 1.5:
        return "excellent"
    elif avg_hours >= 7.0:
        return "good"
    elif avg_hours >= 6.0:
        return "fair"
    else:
        return "poor"


# --- 회복 준비도 평가 ---


def assess_recovery_readiness(
    recovery: RecoveryScore,
    target_muscle_groups: list[str] | None = None,
) -> dict:
    """다음 세션을 위한 회복 준비도 평가."""
    readiness = {
        "recovery_score": recovery.score,
        "fatigue_level": recovery.fatigue_level,
        "recommended_intensity": "중강도",  # default
        "can_train": True,
        "warnings": [],
        "suggestions": [],
    }

    # 강도 캡 결정
    if recovery.score >= 80:
        readiness["recommended_intensity"] = "최대강도"
        readiness["suggestions"].append("회복 양호. PR 도전 가능")
    elif recovery.score >= 60:
        readiness["recommended_intensity"] = "고강도"
        readiness["suggestions"].append("정상 훈련 진행 가능")
    elif recovery.score >= 40:
        readiness["recommended_intensity"] = "중강도"
        readiness["warnings"].append("피로 누적 감지. 볼륨 유지, RPE 7 이하 권장")
    elif recovery.score >= 20:
        readiness["recommended_intensity"] = "저강도"
        readiness["warnings"].append("피로 높음. 경량 훈련 또는 더리셋 권장")
    else:
        readiness["recommended_intensity"] = "휴식"
        readiness["can_train"] = False
        readiness["warnings"].append("피로 극심. 완전 휴식 필수")

    if recovery.deload_recommended:
        readiness["warnings"].append("디로드 주간 권장: 볼륨 40-50% 감소")

    if recovery.sleep_quality == "poor":
        readiness["warnings"].append("수면 품질 저하. 수면 개선 최우선")

    if recovery.consecutive_training_days >= 4:
        readiness["warnings"].append(
            f"연속 {recovery.consecutive_training_days}일 훈련. "
            "내일 반드시 휴식 또는 더리셋"
        )

    return readiness


# --- 트렌드 분석 ---


def analyze_sleep_trend(sleep_logs: list[SleepLog], days: int = 7) -> dict:
    """수면 트렌드 분석 (3-7일)."""
    if len(sleep_logs) < 3:
        return {"status": "insufficient_data", "message": "최소 3일 수면 데이터 필요"}

    recent = sorted(sleep_logs, key=lambda s: s.date, reverse=True)[:days]
    recent.reverse()  # 시간순

    avg_hours = sum(s.duration_hours for s in recent) / len(recent)
    hours_list = [s.duration_hours for s in recent]
    consistency = max(hours_list) - min(hours_list) if len(hours_list) >= 2 else 0

    trend = {
        "period_days": len(recent),
        "avg_hours": round(avg_hours, 1),
        "min_hours": round(min(hours_list), 1),
        "max_hours": round(max(hours_list), 1),
        "consistency_range": round(consistency, 1),
    }

    # 리스크
    risks = []
    if avg_hours < 6:
        risks.append(f"평균 수면 {trend['avg_hours']}시간 — 6시간 미만. 심각한 회복 저하")
    elif avg_hours < 7:
        risks.append(f"평균 수면 {trend['avg_hours']}시간 — 7시간 미만. 퍼포먼스 저하 가능")

    if consistency > 2:
        risks.append(f"수면 시간 편차 {trend['consistency_range']}시간 — 일관성 부족. 생체리듬 교란")

    # 품질 트렌드 (있으면)
    quality_logs = [s for s in recent if s.quality is not None]
    if quality_logs:
        avg_quality = sum(s.quality for s in quality_logs) / len(quality_logs)
        trend["avg_quality"] = round(avg_quality, 1)
        if avg_quality < 3:
            risks.append(f"수면 품질 평균 {trend['avg_quality']}/5 — 수면 환경 개선 필요")

    trend["risks"] = risks
    return trend


# --- 출력 렌더링 ---


def render_recovery_score(recovery: RecoveryScore) -> str:
    """회복 점수 텍스트 렌더링."""
    lines = [f"## 회복 상태 ({recovery.date})"]
    lines.append(f"회복 점수: {recovery.score}/100")
    lines.append(f"피로 수준: {recovery.fatigue_level}")
    lines.append(f"수면 품질: {recovery.sleep_quality}")
    lines.append(f"연속 훈련일: {recovery.consecutive_training_days}일")

    if recovery.deload_recommended:
        lines.append("\n⚠ 디로드 권장")

    if recovery.details:
        d = recovery.details
        lines.append(f"\n### 상세 (피로 = {d.get('total_fatigue', '?')}/100)")
        lines.append(f"  수면부채: {d.get('sleep_debt_score', '?')}/30")
        lines.append(f"  훈련부하: {d.get('training_load_score', '?')}/30")
        lines.append(f"  연속훈련: {d.get('consecutive_days_score', '?')}/20")
        lines.append(f"  주관피로: {d.get('subjective_score', '?')}/10")
        lines.append(f"  회복위반: {d.get('violation_score', '?')}/10")

    return "\n".join(lines)
