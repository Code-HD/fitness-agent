"""Claude API 호출 래퍼 — 코치별 프롬프트 라우팅 + 인사이트 엔진 통합.

3가지 모드:
  1. 세션 인사이트: 세션 직후 로컬 분석 (API 불필요)
  2. 주간 인사이트: 로컬 분석 + AI 보강 (API 선택)
  3. 기간 인사이트: 로컬 분석 + AI 심층 분석 (API 선택)

코치별 프롬프트:
  - Exercise Coach: 세션/운동 분석 시
  - Nutrition Coach: 영양 분석 시
  - Recovery Coach: 회복/수면 분석 시
  - Orchestrator: 통합 인사이트 (주간 리포트, 기간 분석)
"""

from __future__ import annotations

import json
import os
from typing import Optional

from core.analyzer import AnalysisResult
from core.coaches.prompts import (
    EXERCISE_COACH_PROMPT,
    NUTRITION_COACH_PROMPT,
    ORCHESTRATOR_PROMPT,
    RECOVERY_COACH_PROMPT,
)
from core.insight_engine import (
    PeriodInsight,
    SessionInsight,
    WeeklyInsight,
    render_period_insight,
    render_session_insight,
    render_weekly_insight,
)

# Legacy prompt (kept for backward compat, orchestrator prompt used for new flows)
SYSTEM_PROMPT = ORCHESTRATOR_PROMPT


def _get_coach_prompt(domain: str = "orchestrator") -> str:
    """코치별 시스템 프롬프트 반환."""
    prompts = {
        "exercise": EXERCISE_COACH_PROMPT,
        "nutrition": NUTRITION_COACH_PROMPT,
        "recovery": RECOVERY_COACH_PROMPT,
        "orchestrator": ORCHESTRATOR_PROMPT,
    }
    return prompts.get(domain, ORCHESTRATOR_PROMPT)

PERIOD_PROMPT_TEMPLATE = """\
운동 데이터 분석 결과:

{context}

{insight_summary}

위 분석 결과를 바탕으로 종합 인사이트를 제공해주세요.
특히 다음에 주목해주세요:
{focus_points}

{user_question}
"""


def _build_enhanced_context(result: AnalysisResult) -> str:
    """인사이트 엔진 결과 포함 강화 컨텍스트."""
    lines = [
        f"## 분석 기간: 최근 {result.period_days}일",
        f"총 세션: {result.total_sessions}",
        f"세션 구성: {json.dumps(result.session_breakdown, ensure_ascii=False)}",
        "",
    ]

    if result.weekly_volumes:
        lines.append("## 주간 볼륨 추이")
        for wv in result.weekly_volumes:
            lines.append(
                f"  {wv.week_start}: {wv.total_volume:,.0f}kg "
                f"({wv.session_count}세션)"
            )
        lines.append("")

    if result.exercise_progress:
        lines.append("## 운동별 진전")
        for ep in result.exercise_progress:
            change = ep.last_estimated_1rm - ep.first_estimated_1rm
            sign = "+" if change > 0 else ""
            lines.append(f"  {ep.name}:")
            lines.append(
                f"    1RM: {ep.first_estimated_1rm:.1f} → {ep.last_estimated_1rm:.1f}kg "
                f"({sign}{change:.1f}kg) (최고: {ep.best_estimated_1rm:.1f})"
            )
            if ep.stagnation_weeks > 0:
                lines.append(f"    ⚠ 정체: {ep.stagnation_weeks}주")
            if ep.suggestion:
                lines.append(f"    제안: {ep.suggestion}")
        lines.append("")

    if result.overload_alerts:
        lines.append("## ⚠ 과부하 경고")
        for a in result.overload_alerts:
            t = a.exercise_name or "전체"
            lines.append(
                f"  {t}: {a.previous_week_volume:,.0f} → "
                f"{a.current_week_volume:,.0f}kg (+{a.increase_pct:.1f}%)"
            )
        lines.append("")

    # 근육군 볼륨
    if result.muscle_group_volumes:
        lines.append("## 근육군 볼륨")
        total = sum(result.muscle_group_volumes.values()) or 1
        for group, vol in sorted(
            result.muscle_group_volumes.items(),
            key=lambda x: x[1], reverse=True,
        ):
            pct = vol / total * 100
            lines.append(f"  {group}: {vol:,.0f}kg ({pct:.0f}%)")
        lines.append("")

    if result.body_trend:
        bt = result.body_trend
        lines.append("## 체성분")
        lines.append(f"  현재: {bt.recent_weight}kg")
        if bt.weight_change_weekly is not None:
            s = "+" if bt.weight_change_weekly > 0 else ""
            lines.append(f"  주간: {s}{bt.weight_change_weekly:.1f}kg")
        if bt.weight_change_monthly is not None:
            s = "+" if bt.weight_change_monthly > 0 else ""
            lines.append(f"  월간: {s}{bt.weight_change_monthly:.1f}kg")
        if bt.body_fat_change is not None:
            s = "+" if bt.body_fat_change > 0 else ""
            lines.append(f"  체지방 변화: {s}{bt.body_fat_change:.1f}%")
        if bt.volume_vs_weight:
            lines.append(f"  종합: {bt.volume_vs_weight}")

    return "\n".join(lines)


def _build_focus_points(result: AnalysisResult) -> str:
    """분석 결과에서 AI가 주목해야 할 포인트 동적 생성."""
    points = []

    # 과부하 경고 있으면
    if result.overload_alerts:
        points.append("- 과부하 경고: 부상 방지를 위한 구체적 조언")

    # 정체기 운동
    stagnant = [ep for ep in result.exercise_progress if ep.stagnation_weeks >= 3]
    if stagnant:
        names = ", ".join(ep.name for ep in stagnant[:3])
        points.append(f"- 정체기 운동({names}): 돌파를 위한 구체적 전략")

    # 기간 인사이트
    pi = result.period_insight
    if pi:
        if pi.improving_exercises:
            points.append("- 성장 중인 운동: 모멘텀 유지 방법")
        if pi.declining_exercises:
            points.append("- 약세 운동: 원인 분석과 대처")
        if pi.consistency_pct < 70:
            points.append("- 훈련 일관성: 규칙적 습관 형성 방법")

    # 체성분
    if result.body_trend:
        if result.body_trend.body_fat_change is not None:
            points.append("- 체성분 변화와 운동 볼륨의 상관관계")

    if not points:
        points.append("- 현재 상태에서 다음 단계로 가기 위한 구체적 행동 계획")

    return "\n".join(points)


def generate_insight(
    result: AnalysisResult,
    user_question: Optional[str] = None,
    model: str = "claude-sonnet-4-20250514",
) -> str:
    """기간별 종합 인사이트 생성."""
    context = _build_enhanced_context(result)

    # 인사이트 엔진의 기간 분석 결과
    insight_summary = ""
    if result.period_insight:
        insight_summary = render_period_insight(result.period_insight)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _fallback_insight(result, context, insight_summary)

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        focus = _build_focus_points(result)

        question_part = ""
        if user_question:
            question_part = f"추가 질문: {user_question}"
        else:
            question_part = "종합 인사이트와 구체적 행동 제안을 해주세요."

        msg = PERIOD_PROMPT_TEMPLATE.format(
            context=context,
            insight_summary=insight_summary,
            focus_points=focus,
            user_question=question_part,
        )

        resp = client.messages.create(
            model=model,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": msg}],
        )
        return resp.content[0].text
    except Exception as e:
        return _fallback_insight(result, context, insight_summary, error=str(e))


def generate_session_narrative(
    session_insight: SessionInsight,
    model: str = "claude-sonnet-4-20250514",
) -> Optional[str]:
    """세션 인사이트에 AI 내러티브 추가 (선택적)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    rendered = render_session_insight(session_insight)
    if not session_insight.prs and not session_insight.warnings:
        return None  # 특별한 게 없으면 AI 호출 안 함

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        msg = (
            f"세션 분석 결과:\n\n{rendered}\n\n"
            "위 결과를 바탕으로 1-2문장의 코치 코멘트를 작성해주세요. "
            "칭찬할 점은 칭찬하고, 주의할 점은 간결하게 경고해주세요."
        )
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            system=_get_coach_prompt("exercise"),
            messages=[{"role": "user", "content": msg}],
        )
        return resp.content[0].text
    except Exception:
        return None


def generate_weekly_narrative(
    weekly_insight: WeeklyInsight,
    model: str = "claude-sonnet-4-20250514",
) -> Optional[str]:
    """주간 인사이트에 AI 내러티브 추가 (선택적)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    rendered = render_weekly_insight(weekly_insight)

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        msg = (
            f"주간 분석 결과:\n\n{rendered}\n\n"
            "위 결과를 바탕으로 이번 주 총평과 다음 주 핵심 포인트를 "
            "3-4문장으로 작성해주세요."
        )
        resp = client.messages.create(
            model=model,
            max_tokens=400,
            system=_get_coach_prompt("orchestrator"),
            messages=[{"role": "user", "content": msg}],
        )
        return resp.content[0].text
    except Exception:
        return None


def generate_coach_response(
    domain: str,
    context: str,
    question: str = "",
    model: str = "claude-sonnet-4-20250514",
) -> Optional[str]:
    """특정 코치 도메인으로 AI 응답 생성.

    Args:
        domain: "exercise", "nutrition", "recovery", "orchestrator"
        context: 분석 데이터 컨텍스트
        question: 사용자 질문 또는 분석 요청
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        prompt = _get_coach_prompt(domain)
        msg = f"{context}\n\n{question}" if question else context

        resp = client.messages.create(
            model=model,
            max_tokens=800,
            system=prompt,
            messages=[{"role": "user", "content": msg}],
        )
        return resp.content[0].text
    except Exception:
        return None


def _fallback_insight(
    result: AnalysisResult,
    context: str,
    insight_summary: str = "",
    error: Optional[str] = None,
) -> str:
    """AI 미연동 시 로컬 분석 기반 인사이트."""
    lines = []
    if error:
        lines.append(f"(AI 연동 실패: {error})\n")
    else:
        lines.append("(AI 미연동 — 로컬 분석)\n")

    # 인사이트 엔진 결과가 있으면 우선 사용
    if insight_summary:
        lines.append(insight_summary)
    else:
        lines.append(context)

    lines.append("")

    for ep in result.exercise_progress:
        if ep.stagnation_weeks >= 3:
            lines.append(
                f"💡 {ep.name}: {ep.stagnation_weeks}주 정체. "
                "중량 2.5kg↑ 또는 볼륨 변화 시도"
            )

    for a in result.overload_alerts:
        t = a.exercise_name or "전체"
        lines.append(f"⚠️ {t}: 전주 대비 {a.increase_pct:.0f}%↑ — 부상 주의")

    if result.body_trend and result.body_trend.volume_vs_weight:
        lines.append(f"\n📊 {result.body_trend.volume_vs_weight}")

    return "\n".join(lines)
