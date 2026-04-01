"""오운동 시리즈 MCP 서버 — 인사이트 엔진 통합.

Claude Desktop에서 대화로:
  "오늘 스쿼트 100x5 3세트 기록해줘"  → 기록 + 자동 인사이트
  "이번 주 리포트 보여줘"              → 주간 리포트 + 인사이트
  "30일 분석해줘"                     → 기간별 심층 분석
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastmcp import FastMCP

from core.db import get_adapter
from core.models import (
    BodyMetrics,
    Exercise,
    ExerciseSet,
    Meal,
    NutritionTarget,
    Session,
    SessionType,
    SleepLog,
    WeeklyPlan,
    parse_sets,
)
from core.analyzer import Analyzer
from core.ai import generate_insight, generate_session_narrative, generate_weekly_narrative
from core.insight_engine import (
    InsightEngine,
    render_session_insight,
    render_weekly_insight,
    render_period_insight,
)
from core.inbody_parser import parse_inbody_screenshot
from core.firestore_sync import sync_session, sync_body_metrics

# Coach modules
from core.coaches.nutrition import (
    aggregate_daily_nutrition,
    analyze_nutrition_trend,
    analyze_vs_target,
    estimate_macros,
    render_daily_summary,
)
from core.coaches.recovery import (
    analyze_sleep_trend,
    assess_recovery_readiness,
    calculate_fatigue_score,
    render_recovery_score,
)

mcp = FastMCP(
    "오운동 시리즈",
    instructions=(
        "개인 운동 기록 에이전트. "
        "오운동 시리즈 체계: 주간Plan(Week N) > 세션(오운동-N, 더리셋) > 운동 > 세트. "
        "한 세션에 웨이트, 유산소, 필라테스 혼합 가능. "
        "세트 형식: '100x5'(중량), '42s'(시간), '7'(맨몸), '15kg 10&10(R)'(양측). "
        "더리셋 = 필라테스 세션. 출력은 한국어. "
        "모든 기록 후 자동으로 PR 감지, 볼륨 비교, 피로도 분석 인사이트를 제공합니다."
    ),
)

engine = InsightEngine()
analyzer = Analyzer()


# ─── 기록 도구 ───


@mcp.tool()
def log_session(
    week: int,
    session_number: int,
    date_str: str,
    exercises: list[dict],
    year: int | None = None,
    note: str = "",
) -> str:
    """오운동 세션 기록 + 자동 인사이트.

    Args:
        week: 주차 번호 (예: 9)
        session_number: 세션 번호 (오운동-1, 오운동-2 등)
        date_str: 날짜 (YYYY-MM-DD)
        exercises: 운동 목록. 각 항목은 {"name": "운동명", "sets": "세트기록"} 형태.
            sets 형식: "100x5, 105x3" (중량), "42s, 27s" (시간), "7, 6, 5" (맨몸)
        year: 연도 (기본: 올해)
        note: 메모
    """
    adapter = get_adapter()
    yr = year or date.today().year
    session_date = date.fromisoformat(date_str)

    plan = adapter.get_plan(yr, week)
    if not plan:
        plan = WeeklyPlan(year=yr, week_number=week)
        plan_id = adapter.save_plan(plan)
    else:
        plan_id = plan.id

    exercise_list = []
    for i, ex in enumerate(exercises, start=1):
        sets = parse_sets(ex["sets"]) if ex.get("sets") else []
        exercise_list.append(Exercise(name=ex["name"], order=i, sets=sets))

    session = Session(
        date=session_date,
        session_type=SessionType.WORKOUT,
        session_number=session_number,
        plan_id=plan_id,
        exercises=exercise_list,
        note=note,
    )
    sid = adapter.save_session(session)
    session.id = sid

    # 기록 확인
    days = ["월", "화", "수", "목", "금", "토", "일"]
    lines = [f"✓ 오운동-{session_number} | {session_date} ({days[session_date.weekday()]}) | Week {week}"]
    for ex in exercise_list:
        line = f"  * {ex.name}"
        if ex.sets:
            parts = []
            for s in ex.sets:
                if s.weight_kg and s.reps:
                    parts.append(f"{s.weight_kg}kg×{s.reps}")
                elif s.duration_sec:
                    if s.duration_sec < 60:
                        parts.append(f"{s.duration_sec:.0f}s")
                    else:
                        parts.append(f"{s.duration_sec/60:.0f}min")
                elif s.reps:
                    parts.append(f"{s.reps}")
            line += f" — {' / '.join(parts)}"
        lines.append(line)

    if session.total_volume > 0:
        lines.append(f"  총 볼륨: {session.total_volume:,.0f} kg")

    # ── 자동 인사이트 ──
    insight_text = _generate_session_insight(session)
    if insight_text:
        lines.append("")
        lines.append(insight_text)

    # ── Firestore 동기화 ──
    try:
        fs_ok = sync_session(session, week=week, insight=insight_text or "")
        if fs_ok:
            lines.append("\n☁️ 앱 동기화 완료")
        else:
            lines.append("\n⚠️ 앱 동기화 실패 (Firebase CLI 로그인 확인)")
    except Exception as e:
        lines.append(f"\n⚠️ 앱 동기화 오류: {e}")

    return "\n".join(lines)


@mcp.tool()
def log_reset(
    week: int,
    date_str: str,
    exercises: list[dict],
    year: int | None = None,
    note: str = "",
) -> str:
    """더리셋(필라테스) 세션 기록.

    Args:
        week: 주차 번호
        date_str: 날짜 (YYYY-MM-DD)
        exercises: 운동 목록. {"name": "운동명"} 형태 (sets 선택)
        year: 연도
        note: 메모
    """
    adapter = get_adapter()
    yr = year or date.today().year
    session_date = date.fromisoformat(date_str)

    plan = adapter.get_plan(yr, week)
    if not plan:
        plan = WeeklyPlan(year=yr, week_number=week)
        plan_id = adapter.save_plan(plan)
    else:
        plan_id = plan.id

    exercise_list = []
    for i, ex in enumerate(exercises, start=1):
        sets = parse_sets(ex["sets"]) if ex.get("sets") else []
        exercise_list.append(Exercise(name=ex["name"], order=i, sets=sets))

    session = Session(
        date=session_date,
        session_type=SessionType.RESET,
        plan_id=plan_id,
        exercises=exercise_list,
        note=note,
    )
    sid = adapter.save_session(session)
    session.id = sid

    days = ["월", "화", "수", "목", "금", "토", "일"]
    lines = [f"✓ 더리셋 | {session_date} ({days[session_date.weekday()]}) | Week {week}"]
    for ex in exercise_list:
        line = f"  * {ex.name}"
        if ex.sets:
            parts = []
            for s in ex.sets:
                if s.duration_sec:
                    if s.duration_sec < 60:
                        parts.append(f"{s.duration_sec:.0f}s")
                    else:
                        parts.append(f"{s.duration_sec/60:.0f}min")
                elif s.reps:
                    parts.append(f"{s.reps}")
            if parts:
                line += f" — {' / '.join(parts)}"
        lines.append(line)

    # 더리셋도 간단 인사이트 (연속 훈련 체크 등)
    insight_text = _generate_session_insight(session)
    if insight_text:
        lines.append("")
        lines.append(insight_text)

    # ── Firestore 동기화 ──
    try:
        fs_ok = sync_session(session, week=week, insight=insight_text or "")
        if fs_ok:
            lines.append("\n☁️ 앱 동기화 완료")
        else:
            lines.append("\n⚠️ 앱 동기화 실패")
    except Exception as e:
        lines.append(f"\n⚠️ 앱 동기화 오류: {e}")

    return "\n".join(lines)


@mcp.tool()
def log_body(
    weight_kg: float,
    date_str: str | None = None,
    body_fat_pct: float | None = None,
    skeletal_muscle_kg: float | None = None,
    bmi: float | None = None,
    body_fat_kg: float | None = None,
    note: str = "",
) -> str:
    """체성분 기록.

    Args:
        weight_kg: 체중 (kg)
        date_str: 날짜 (YYYY-MM-DD, 기본: 오늘)
        body_fat_pct: 체지방률 (%)
        skeletal_muscle_kg: 골격근량 (kg)
        bmi: BMI
        body_fat_kg: 체지방량 (kg)
        note: 메모
    """
    adapter = get_adapter()
    d = date.fromisoformat(date_str) if date_str else date.today()
    metrics = BodyMetrics(
        date=d, weight_kg=weight_kg, body_fat_pct=body_fat_pct,
        skeletal_muscle_kg=skeletal_muscle_kg, bmi=bmi,
        body_fat_kg=body_fat_kg, note=note,
    )
    adapter.save_body_metrics(metrics)

    line = f"✓ 체성분 | {d} | {weight_kg}kg"
    if body_fat_pct:
        line += f" | 체지방 {body_fat_pct}%"
    if skeletal_muscle_kg:
        line += f" | 골격근 {skeletal_muscle_kg}kg"
    if bmi:
        line += f" | BMI {bmi}"

    # 이전 기록과 비교
    prev = adapter.get_body_metrics(
        start_date=d - timedelta(days=30), end_date=d, limit=10,
    )
    prev = [m for m in prev if m.date < d]
    if prev:
        prev.sort(key=lambda m: m.date, reverse=True)
        lp = prev[0]
        diff = weight_kg - lp.weight_kg
        sign = "+" if diff > 0 else ""
        line += f"\n  체중: {sign}{diff:.1f}kg ({lp.date} 대비)"
        if skeletal_muscle_kg and lp.skeletal_muscle_kg:
            md = skeletal_muscle_kg - lp.skeletal_muscle_kg
            ms = "+" if md > 0 else ""
            line += f" | 골격근: {ms}{md:.1f}kg"
        if body_fat_pct and lp.body_fat_pct:
            fd = body_fat_pct - lp.body_fat_pct
            fs = "+" if fd > 0 else ""
            line += f" | 체지방률: {fs}{fd:.1f}%"

    # ── Firestore 동기화 ──
    try:
        fs_ok = sync_body_metrics(metrics)
        if fs_ok:
            line += "\n☁️ 앱 동기화 완료"
        else:
            line += "\n⚠️ 앱 동기화 실패"
    except Exception as e:
        line += f"\n⚠️ 앱 동기화 오류: {e}"

    return line


@mcp.tool()
def parse_inbody(
    image_path: str | None = None,
    image_base64: str | None = None,
    date_str: str | None = None,
    save: bool = True,
) -> str:
    """InBody 스크린샷에서 체성분 데이터 자동 추출 + 저장.

    InBody 앱 캡쳐 이미지를 올리면 자동으로 체중, 골격근량, 체지방률 등을 파싱합니다.

    Args:
        image_path: 이미지 파일 경로
        image_base64: base64 인코딩 이미지 (경로 대신 사용 가능)
        date_str: 측정 날짜 (YYYY-MM-DD, 이미지에서 읽거나 기본 오늘)
        save: True면 DB에 자동 저장
    """
    result = parse_inbody_screenshot(
        image_path=image_path, image_base64=image_base64,
    )

    if not result:
        return (
            "InBody 데이터 추출 실패.\n"
            "- ANTHROPIC_API_KEY 환경변수 확인\n"
            "- 이미지가 InBody 결과 화면인지 확인\n"
            "- 직접 입력: log_body(weight_kg=73, body_fat_pct=18, skeletal_muscle_kg=30)"
        )

    # 날짜 결정
    d = None
    if date_str:
        d = date.fromisoformat(date_str)
    elif result.measurement_date:
        d = result.measurement_date
    else:
        d = date.today()

    lines = [f"📋 InBody 결과 파싱 완료 ({d})\n"]
    lines.append(f"  체중: {result.weight_kg}kg")
    if result.skeletal_muscle_kg:
        lines.append(f"  골격근량: {result.skeletal_muscle_kg}kg")
    if result.body_fat_pct:
        lines.append(f"  체지방률: {result.body_fat_pct}%")
    if result.body_fat_kg:
        lines.append(f"  체지방량: {result.body_fat_kg}kg")
    if result.bmi:
        lines.append(f"  BMI: {result.bmi}")

    if save:
        adapter = get_adapter()
        metrics = BodyMetrics(
            date=d, weight_kg=result.weight_kg,
            body_fat_pct=result.body_fat_pct,
            skeletal_muscle_kg=result.skeletal_muscle_kg,
            bmi=result.bmi,
            body_fat_kg=result.body_fat_kg,
            note="InBody 자동 파싱",
        )
        adapter.save_body_metrics(metrics)
        lines.append("\n✓ DB에 저장 완료")

        # Firestore 동기화
        try:
            if sync_body_metrics(metrics):
                lines.append("☁️ 앱 동기화 완료")
        except Exception:
            pass

        # 이전 기록 비교
        prev = adapter.get_body_metrics(
            start_date=d - timedelta(days=60), end_date=d, limit=10,
        )
        prev = [m for m in prev if m.date < d]
        if prev:
            prev.sort(key=lambda m: m.date, reverse=True)
            lp = prev[0]
            lines.append(f"\n📊 변화 ({lp.date} → {d}):")
            diff = result.weight_kg - lp.weight_kg
            sign = "+" if diff > 0 else ""
            lines.append(f"  체중: {sign}{diff:.1f}kg")
            if result.skeletal_muscle_kg and lp.skeletal_muscle_kg:
                md = result.skeletal_muscle_kg - lp.skeletal_muscle_kg
                ms = "+" if md > 0 else ""
                lines.append(f"  골격근: {ms}{md:.1f}kg")
            if result.body_fat_pct and lp.body_fat_pct:
                fd = result.body_fat_pct - lp.body_fat_pct
                fs = "+" if fd > 0 else ""
                lines.append(f"  체지방률: {fs}{fd:.1f}%")

    return "\n".join(lines)


# ─── 조회 + 인사이트 도구 ───


@mcp.tool()
def weekly_report(week: int | None = None, year: int | None = None) -> str:
    """주간 리포트 + 인사이트.

    Args:
        week: 주차 번호 (없으면 이번 주)
        year: 연도
    """
    adapter = get_adapter()

    if week:
        yr = year or date.today().year
        plan = adapter.get_plan(yr, week)
        if not plan:
            return f"Week {week} 기록이 없습니다."
        sessions = plan.sessions
        title = plan.label
    else:
        today = date.today()
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        sessions = adapter.get_sessions(start_date=start, end_date=end, limit=20)
        title = f"주간 리포트 ({start} ~ {end})"

    if not sessions:
        return "이번 주 기록이 없습니다."

    lines = [f"═══ {title} ═══\n"]
    sessions_sorted = sorted(sessions, key=lambda s: (s.date, s.session_number or 99))

    wk_count = sum(1 for s in sessions_sorted if s.session_type == SessionType.WORKOUT)
    rs_count = sum(1 for s in sessions_sorted if s.session_type == SessionType.RESET)
    total_vol = sum(s.total_volume for s in sessions_sorted)

    summary = f"오운동 {wk_count}회"
    if rs_count:
        summary += f" + 더리셋 {rs_count}회"
    summary += f" | 총 볼륨: {total_vol:,.0f} kg"
    lines.append(summary + "\n")

    for s in sessions_sorted:
        lines.append(f"# {s.label} | {s.date} ({s.day_label})")
        for ex in s.exercises:
            parts = []
            for st in ex.sets:
                if st.weight_kg and st.reps:
                    p = f"{st.weight_kg}kg×{st.reps}"
                    if st.side:
                        p += f"({st.side})"
                    parts.append(p)
                elif st.duration_sec:
                    if st.duration_sec < 60:
                        parts.append(f"{st.duration_sec:.0f}s")
                    else:
                        parts.append(f"{st.duration_sec/60:.0f}min")
                elif st.reps:
                    parts.append(f"×{st.reps}")
            line = f"  * {ex.name}"
            if parts:
                line += f" — {' / '.join(parts)}"
            if ex.total_volume > 0:
                line += f"  [{ex.total_volume:,.0f}kg]"
            lines.append(line)
        if s.total_volume > 0:
            lines.append(f"  볼륨: {s.total_volume:,.0f} kg")
        lines.append("")

    # ── 주간 인사이트 ──
    weekly_insight = _generate_weekly_insight(sessions_sorted)
    if weekly_insight:
        lines.append(weekly_insight)

    return "\n".join(lines)


@mcp.tool()
def exercise_progress(exercise_name: str, days: int = 30) -> str:
    """특정 운동의 진행 현황 + 인사이트.

    Args:
        exercise_name: 운동명
        days: 조회 기간 (일)
    """
    adapter = get_adapter()
    today = date.today()
    start = today - timedelta(days=days)
    sessions = adapter.get_sessions(start_date=start, end_date=today, limit=500)

    found = []
    for s in sessions:
        for ex in s.exercises:
            if ex.name.lower() == exercise_name.lower():
                found.append((s, ex))

    if not found:
        return f"최근 {days}일간 '{exercise_name}' 기록 없음."

    found.sort(key=lambda x: x[0].date)
    lines = [f"═══ {exercise_name} (최근 {days}일) ═══\n"]

    for s, ex in found:
        parts = []
        for st in ex.sets:
            if st.weight_kg and st.reps:
                parts.append(f"{st.weight_kg}×{st.reps}")
            elif st.duration_sec:
                parts.append(f"{st.duration_sec:.0f}s")
            elif st.reps:
                parts.append(str(st.reps))
        line = f"  {s.date} ({s.day_label}) [{s.label}]"
        if parts:
            line += f"  {' / '.join(parts)}"
        if ex.total_volume > 0:
            line += f"  볼륨:{ex.total_volume:,.0f}kg"
        ts = ex.top_set
        if ts:
            line += f"  1RM:{ts.estimated_1rm:.1f}"
        lines.append(line)

    # 추세 분석
    weighted = [(s, ex) for s, ex in found if ex.top_set]
    if len(weighted) >= 2:
        f1 = weighted[0][1].top_set.estimated_1rm
        l1 = weighted[-1][1].top_set.estimated_1rm
        best = max(ex.top_set.estimated_1rm for _, ex in weighted)
        ch = l1 - f1
        pct = (ch / f1 * 100) if f1 > 0 else 0
        sign = "+" if ch > 0 else ""
        lines.append(f"\n추정 1RM: {f1:.1f} → {l1:.1f} ({sign}{pct:.1f}%)")
        lines.append(f"최고 1RM: {best:.1f}kg")

        # 정체 감지
        e1rms = [(s.date, ex.top_set.estimated_1rm) for s, ex in weighted]
        stag = engine._detect_stagnation(e1rms)
        if stag >= 3:
            lines.append(f"\n⚠ {stag}주 정체 감지 — 프로그램 변경 고려")
            lines.append("  💡 중량 2.5kg↑, 렙 스킴 변경, 또는 보조 운동 추가")
        elif stag == 2:
            lines.append(f"\n📌 2주 동일 수준 — 다음 세션에서 도전 권장")

    # 볼륨 추이
    vols = [ex.total_volume for _, ex in found if ex.total_volume > 0]
    if len(vols) >= 3:
        avg_vol = sum(vols) / len(vols)
        recent_avg = sum(vols[-2:]) / min(2, len(vols[-2:]))
        if avg_vol > 0:
            vol_trend = (recent_avg - avg_vol) / avg_vol * 100
            sign = "+" if vol_trend > 0 else ""
            lines.append(f"볼륨 추세: 평균 {avg_vol:,.0f}kg → 최근 {recent_avg:,.0f}kg ({sign}{vol_trend:.0f}%)")

    return "\n".join(lines)


@mcp.tool()
def get_insight(
    period: str = "30d",
    exercise: str | None = None,
    question: str | None = None,
) -> str:
    """AI 인사이트 생성 — 종합 분석.

    Args:
        period: 분석 기간 (예: 7d, 2w, 1m)
        exercise: 특정 운동 분석 (선택)
        question: 추가 질문 (선택)
    """
    adapter = get_adapter()

    p = period.strip().lower()
    if p.endswith("d"):
        days = int(p[:-1])
    elif p.endswith("w"):
        days = int(p[:-1]) * 7
    elif p.endswith("m"):
        days = int(p[:-1]) * 30
    else:
        days = int(p)

    today = date.today()
    start = today - timedelta(days=days)
    sessions = adapter.get_sessions(start_date=start, end_date=today, limit=1000)
    body = adapter.get_body_metrics(start_date=start, end_date=today, limit=100)

    if not sessions and not body:
        return f"최근 {days}일간 기록이 없습니다."

    result = analyzer.analyze(sessions, body, period_days=days)
    return generate_insight(result, user_question=question)


@mcp.tool()
def session_insight(session_id: int | None = None) -> str:
    """가장 최근 세션(또는 지정 세션)의 상세 인사이트.

    Args:
        session_id: 세션 ID (없으면 가장 최근 세션)
    """
    adapter = get_adapter()

    if session_id:
        session = adapter.get_session_by_id(session_id)
        if not session:
            return f"세션 ID {session_id} 없음."
    else:
        recent = adapter.get_sessions(limit=1)
        if not recent:
            return "기록된 세션이 없습니다."
        session = recent[0]

    # 전체 이력 조회
    all_history = adapter.get_sessions(limit=500)
    si = engine.session_insight(session, all_history)

    return render_session_insight(si)


@mcp.tool()
def list_exercises() -> str:
    """기록된 모든 운동명 목록 조회."""
    adapter = get_adapter()
    names = adapter.get_exercise_names()
    if not names:
        return "기록된 운동이 없습니다."
    return "기록된 운동:\n" + "\n".join(f"  - {n}" for n in names)


@mcp.tool()
def body_history(days: int = 90) -> str:
    """체성분 기록 이력 + 추세 분석.

    Args:
        days: 조회 기간 (일)
    """
    adapter = get_adapter()
    today = date.today()
    start = today - timedelta(days=days)
    records = adapter.get_body_metrics(start_date=start, end_date=today, limit=100)

    if not records:
        return f"최근 {days}일간 체성분 기록 없음."

    records.sort(key=lambda m: m.date)
    lines = [f"═══ 체성분 이력 (최근 {days}일) ═══\n"]
    for m in records:
        line = f"  {m.date} | {m.weight_kg}kg"
        if m.skeletal_muscle_kg:
            line += f" | 골격근 {m.skeletal_muscle_kg}kg"
        if m.body_fat_pct:
            line += f" | 체지방 {m.body_fat_pct}%"
        if m.bmi:
            line += f" | BMI {m.bmi}"
        if m.note:
            line += f" | {m.note}"
        lines.append(line)

    if len(records) >= 2:
        first, last = records[0], records[-1]
        diff = last.weight_kg - first.weight_kg
        sign = "+" if diff > 0 else ""
        lines.append(f"\n변화: {first.weight_kg} → {last.weight_kg} ({sign}{diff:.1f}kg)")

        # 주간 변화율
        days_span = max(1, (last.date - first.date).days)
        weekly_rate = diff / (days_span / 7)
        sign_w = "+" if weekly_rate > 0 else ""
        lines.append(f"주간 변화율: {sign_w}{weekly_rate:.2f}kg/주")

        # 체지방 추세
        bf_records = [m for m in records if m.body_fat_pct is not None]
        if len(bf_records) >= 2:
            bf_diff = bf_records[-1].body_fat_pct - bf_records[0].body_fat_pct
            bf_sign = "+" if bf_diff > 0 else ""
            lines.append(
                f"체지방: {bf_records[0].body_fat_pct}% → "
                f"{bf_records[-1].body_fat_pct}% ({bf_sign}{bf_diff:.1f}%)"
            )

    return "\n".join(lines)


# ─── 영양 코치 도구 ───


@mcp.tool()
def log_meal(
    meal_type: str,
    description: str,
    date_str: str | None = None,
    calories: float | None = None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
    note: str = "",
) -> str:
    """식사 기록 + AI 매크로 추정.

    Args:
        meal_type: 식사 유형 (아침/점심/저녁/간식/운동전/운동후)
        description: 음식 설명 ("닭가슴살 200g + 현미밥 1공기")
        date_str: 날짜 (YYYY-MM-DD, 기본: 오늘)
        calories: 칼로리 (직접 입력 시)
        protein_g: 단백질 (직접 입력 시)
        carbs_g: 탄수화물 (직접 입력 시)
        fat_g: 지방 (직접 입력 시)
        note: 메모
    """
    adapter = get_adapter()
    d = date_str or date.today().isoformat()

    # AI 매크로 추정 (값이 없는 경우)
    if calories is None:
        estimated = estimate_macros(description)
        calories = estimated.get("calories")
        protein_g = protein_g or estimated.get("protein_g")
        carbs_g = carbs_g or estimated.get("carbs_g")
        fat_g = fat_g or estimated.get("fat_g")

    meal = Meal(
        date=d, meal_type=meal_type, description=description,
        calories=calories, protein_g=protein_g, carbs_g=carbs_g,
        fat_g=fat_g, note=note,
    )
    mid = adapter.save_meal(meal)

    lines = [f"✓ {meal_type} 기록 | {d}"]
    lines.append(f"  {description}")
    if calories:
        lines.append(f"  {calories:.0f}kcal | P:{protein_g or 0:.0f}g C:{carbs_g or 0:.0f}g F:{fat_g or 0:.0f}g")

    # 일일 현황
    day_meals = adapter.get_meals(
        start_date=date.fromisoformat(d),
        end_date=date.fromisoformat(d),
    )
    if day_meals:
        daily = aggregate_daily_nutrition(day_meals, d)
        lines.append(f"\n  일일 합계: {daily.total_calories:.0f}kcal | "
                     f"P:{daily.total_protein_g:.0f}g C:{daily.total_carbs_g:.0f}g F:{daily.total_fat_g:.0f}g")

    return "\n".join(lines)


@mcp.tool()
def daily_nutrition(date_str: str | None = None) -> str:
    """일일 영양 요약 + 목표 대비 달성률.

    Args:
        date_str: 날짜 (YYYY-MM-DD, 기본: 오늘)
    """
    adapter = get_adapter()
    d = date_str or date.today().isoformat()
    d_date = date.fromisoformat(d)

    meals = adapter.get_meals(start_date=d_date, end_date=d_date)
    if not meals:
        return f"{d} 식사 기록 없음."

    daily = aggregate_daily_nutrition(meals, d)

    # 영양 목표 조회
    target = adapter.get_nutrition_target(target_date=d_date)

    # 체중 조회 (목표 대비 분석용)
    body = adapter.get_body_metrics(limit=1)
    weight_kg = body[0].weight_kg if body else 88.0  # fallback

    if target:
        analysis = analyze_vs_target(daily, target, weight_kg)
        lines = [render_daily_summary(daily, analysis)]
        if analysis.get("risks"):
            lines.append("\n⚠ 리스크:")
            for r in analysis["risks"]:
                lines.append(f"  - {r}")
        return "\n".join(lines)
    else:
        return render_daily_summary(daily)


@mcp.tool()
def nutrition_insight(period: str = "7d") -> str:
    """영양 트렌드 분석.

    Args:
        period: 분석 기간 (7d/2w/1m)
    """
    adapter = get_adapter()

    p = period.strip().lower()
    if p.endswith("d"):
        days = int(p[:-1])
    elif p.endswith("w"):
        days = int(p[:-1]) * 7
    elif p.endswith("m"):
        days = int(p[:-1]) * 30
    else:
        days = int(p)

    today = date.today()
    start = today - timedelta(days=days)
    meals = adapter.get_meals(start_date=start, end_date=today)

    if not meals:
        return f"최근 {days}일간 식사 기록 없음."

    target = adapter.get_nutrition_target()
    body = adapter.get_body_metrics(limit=1)
    weight_kg = body[0].weight_kg if body else 88.0

    # 일별 집계
    from collections import defaultdict
    by_date: dict[str, list] = defaultdict(list)
    for m in meals:
        by_date[m.date].append(m)

    daily_summaries = []
    for d_str in sorted(by_date.keys()):
        daily_summaries.append(aggregate_daily_nutrition(by_date[d_str], d_str))

    trend = analyze_nutrition_trend(daily_summaries, target, weight_kg)
    lines = [f"═══ 영양 트렌드 (최근 {days}일) ═══\n"]
    lines.append(f"기록 일수: {len(daily_summaries)}일")

    if trend.get("status") == "insufficient_data":
        lines.append(f"\n{trend.get('message', '데이터 부족')}")
        # 현재까지의 합계라도 표시
        if daily_summaries:
            avg_cal = sum(d.total_calories for d in daily_summaries) / len(daily_summaries)
            avg_pro = sum(d.total_protein_g for d in daily_summaries) / len(daily_summaries)
            lines.append(f"\n현재 평균: {avg_cal:.0f}kcal | P:{avg_pro:.0f}g")
    else:
        lines.append(f"평균 칼로리: {trend.get('avg_calories', 0):.0f}kcal")
        lines.append(f"평균 단백질: {trend.get('avg_protein', 0):.0f}g")
        lines.append(f"평균 탄수화물: {trend.get('avg_carbs', 0):.0f}g")
        lines.append(f"평균 지방: {trend.get('avg_fat', 0):.0f}g")

        if trend.get("risks"):
            lines.append("\n⚠ 리스크:")
            for r in trend["risks"]:
                lines.append(f"  - {r}")

    return "\n".join(lines)


@mcp.tool()
def set_nutrition_target(
    phase: str,
    calories_target: float,
    protein_g_per_kg: float = 2.0,
    carbs_g_per_kg: float = 4.0,
    fat_g_per_kg: float = 1.0,
    water_ml_target: float = 3000.0,
) -> str:
    """영양 목표 설정/변경.

    Args:
        phase: 목표 페이즈 (bulk/cut/maintain/recomp)
        calories_target: 일일 칼로리 목표 (kcal)
        protein_g_per_kg: 단백질 (g/kg 체중)
        carbs_g_per_kg: 탄수화물 (g/kg 체중)
        fat_g_per_kg: 지방 (g/kg 체중)
        water_ml_target: 일일 수분 목표 (ml)
    """
    adapter = get_adapter()
    target = NutritionTarget(
        phase=phase,
        calories_target=calories_target,
        protein_g_per_kg=protein_g_per_kg,
        carbs_g_per_kg=carbs_g_per_kg,
        fat_g_per_kg=fat_g_per_kg,
        water_ml_target=water_ml_target,
        start_date=date.today().isoformat(),
    )
    tid = adapter.save_nutrition_target(target)

    body = adapter.get_body_metrics(limit=1)
    weight = body[0].weight_kg if body else 88.0

    lines = [f"✓ 영양 목표 설정 ({phase})"]
    lines.append(f"  칼로리: {calories_target:.0f}kcal/일")
    lines.append(f"  단백질: {protein_g_per_kg}g/kg = {protein_g_per_kg * weight:.0f}g/일")
    lines.append(f"  탄수화물: {carbs_g_per_kg}g/kg = {carbs_g_per_kg * weight:.0f}g/일")
    lines.append(f"  지방: {fat_g_per_kg}g/kg = {fat_g_per_kg * weight:.0f}g/일")
    lines.append(f"  수분: {water_ml_target:.0f}ml/일")

    return "\n".join(lines)


# ─── 회복 코치 도구 ───


@mcp.tool()
def log_sleep(
    duration_hours: float,
    date_str: str | None = None,
    sleep_start: str | None = None,
    sleep_end: str | None = None,
    quality: int | None = None,
    note: str = "",
) -> str:
    """수면 기록 + 주간 추세.

    Args:
        duration_hours: 수면 시간 (소수점 가능, 예: 7.5)
        date_str: 날짜 (YYYY-MM-DD, 기본: 오늘)
        sleep_start: 취침 시간 (HH:MM)
        sleep_end: 기상 시간 (HH:MM)
        quality: 수면 품질 (1-5, 주관적)
        note: 메모
    """
    adapter = get_adapter()
    d = date_str or date.today().isoformat()

    log = SleepLog(
        date=d, duration_hours=duration_hours,
        sleep_start=sleep_start, sleep_end=sleep_end,
        quality=quality, note=note,
    )
    adapter.save_sleep_log(log)

    lines = [f"✓ 수면 기록 | {d} | {duration_hours}시간"]
    if sleep_start and sleep_end:
        lines.append(f"  {sleep_start} → {sleep_end}")
    if quality:
        lines.append(f"  품질: {'★' * quality}{'☆' * (5 - quality)} ({quality}/5)")

    # 주간 추세
    d_date = date.fromisoformat(d)
    recent = adapter.get_sleep_logs(
        start_date=d_date - timedelta(days=7),
        end_date=d_date,
    )
    if len(recent) >= 3:
        trend = analyze_sleep_trend(recent, days=7)
        lines.append(f"\n  주간 평균: {trend.get('avg_hours', 0)}시간")
        if trend.get("risks"):
            for r in trend["risks"]:
                lines.append(f"  ⚠ {r}")

    return "\n".join(lines)


@mcp.tool()
def recovery_score(date_str: str | None = None) -> str:
    """회복 점수(0-100) + 피로 경고 + 디로드 권고.

    Args:
        date_str: 날짜 (YYYY-MM-DD, 기본: 오늘)
    """
    adapter = get_adapter()
    d = date_str or date.today().isoformat()
    d_date = date.fromisoformat(d)

    # 최근 수면 기록
    sleep_logs = adapter.get_sleep_logs(
        start_date=d_date - timedelta(days=7),
        end_date=d_date,
    )

    # 최근 세션
    sessions = adapter.get_sessions(
        start_date=d_date - timedelta(days=7),
        end_date=d_date,
    )

    recovery = calculate_fatigue_score(sleep_logs, sessions, target_date=d)
    rendered = render_recovery_score(recovery)

    # 회복 준비도
    readiness = assess_recovery_readiness(recovery)
    lines = [rendered]
    lines.append(f"\n### 훈련 준비도")
    lines.append(f"  권장 강도: {readiness['recommended_intensity']}")
    lines.append(f"  훈련 가능: {'예' if readiness['can_train'] else '아니오'}")

    if readiness["warnings"]:
        lines.append("\n  경고:")
        for w in readiness["warnings"]:
            lines.append(f"    ⚠ {w}")

    if readiness["suggestions"]:
        lines.append("\n  제안:")
        for s in readiness["suggestions"]:
            lines.append(f"    💡 {s}")

    return "\n".join(lines)


@mcp.tool()
def fatigue_check() -> str:
    """피로 종합 분석 (수면 + 훈련부하 + 연속훈련일)."""
    adapter = get_adapter()
    today = date.today()

    sleep_logs = adapter.get_sleep_logs(
        start_date=today - timedelta(days=7),
        end_date=today,
    )
    sessions = adapter.get_sessions(
        start_date=today - timedelta(days=7),
        end_date=today,
    )

    recovery = calculate_fatigue_score(sleep_logs, sessions)
    readiness = assess_recovery_readiness(recovery)

    lines = [f"═══ 피로 종합 분석 ({today.isoformat()}) ═══\n"]

    # Recovery score
    lines.append(f"회복 점수: {recovery.score}/100 ({recovery.fatigue_level})")
    lines.append(f"연속 훈련일: {recovery.consecutive_training_days}일")

    # Sleep trend
    if sleep_logs:
        trend = analyze_sleep_trend(sleep_logs, days=7)
        if trend.get("status") == "insufficient_data":
            lines.append(f"\n수면: 기록 {len(sleep_logs)}일 — {trend.get('message', '데이터 부족')}")
        else:
            lines.append(f"\n수면 (최근 {trend.get('period_days', 0)}일):")
            lines.append(f"  평균: {trend.get('avg_hours', 0)}시간")
            lines.append(f"  범위: {trend.get('min_hours', 0)} ~ {trend.get('max_hours', 0)}시간")
            if trend.get("risks"):
                for r in trend["risks"]:
                    lines.append(f"  ⚠ {r}")
    else:
        lines.append("\n수면 데이터 없음")

    # Training load
    workout_count = sum(1 for s in sessions if s.session_type == SessionType.WORKOUT)
    total_vol = sum(s.total_volume for s in sessions)
    lines.append(f"\n훈련 (최근 7일):")
    lines.append(f"  세션: {workout_count}회")
    lines.append(f"  총 볼륨: {total_vol:,.0f}kg")

    # Fatigue detail
    d = recovery.details
    if d:
        lines.append(f"\n피로 구성 (합계 {d.get('total_fatigue', 0)}/100):")
        lines.append(f"  수면부채: {d.get('sleep_debt_score', 0)}/30")
        lines.append(f"  훈련부하: {d.get('training_load_score', 0)}/30")
        lines.append(f"  연속훈련: {d.get('consecutive_days_score', 0)}/20")
        lines.append(f"  주관피로: {d.get('subjective_score', 0)}/10")
        lines.append(f"  회복위반: {d.get('violation_score', 0)}/10")

    # Recommendation
    lines.append(f"\n권장: {readiness['recommended_intensity']}")
    if readiness["warnings"]:
        for w in readiness["warnings"]:
            lines.append(f"⚠ {w}")

    return "\n".join(lines)


# ─── 내부 헬퍼 ───


def _generate_session_insight(session: Session) -> str:
    """세션 기록 후 자동 인사이트 생성."""
    adapter = get_adapter()

    # 최근 90일 이력 조회 (PR 비교용)
    start = session.date - timedelta(days=90)
    all_history = adapter.get_sessions(start_date=start, end_date=session.date, limit=500)

    si = engine.session_insight(session, all_history)

    # 특별한 게 없으면 최소 인사이트
    if not si.prs and not si.warnings and not si.volume_comparisons:
        # 강도 점수만 표시
        return f"📊 강도: {si.intensity.label} ({si.intensity.score:.0f}/100)"

    return render_session_insight(si)


def _generate_weekly_insight(sessions: list[Session]) -> str:
    """주간 리포트용 인사이트 생성."""
    if not sessions:
        return ""

    adapter = get_adapter()

    # 이번 주 날짜 범위
    dates = sorted(s.date for s in sessions)
    week_start = dates[0] - timedelta(days=dates[0].weekday())
    prev_week_start = week_start - timedelta(days=7)
    prev_week_end = week_start - timedelta(days=1)

    # 이전 주 세션
    prev_sessions = adapter.get_sessions(
        start_date=prev_week_start, end_date=prev_week_end, limit=20,
    )

    # 전체 이력 (이번 주 포함 90일)
    all_start = week_start - timedelta(days=90)
    all_history = adapter.get_sessions(
        start_date=all_start, end_date=dates[-1], limit=500,
    )

    # 체성분
    body = adapter.get_body_metrics(
        start_date=prev_week_start, end_date=dates[-1], limit=20,
    )

    wi = engine.weekly_insight(sessions, prev_sessions, all_history, body or None)
    return render_weekly_insight(wi)


if __name__ == "__main__":
    mcp.run()
