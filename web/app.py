"""오운동 시리즈 웹 대시보드 — FastAPI."""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.db import get_adapter
from core.models import SessionType, parse_sets, Exercise, Session, WeeklyPlan, BodyMetrics
from core.insight_engine import InsightEngine, classify_muscle_group

app = FastAPI(title="오운동 시리즈")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

engine = InsightEngine()


def _get_all_data(days: int = 90):
    """대시보드용 데이터 로드."""
    adapter = get_adapter()
    today = date.today()
    start = today - timedelta(days=days)
    sessions = adapter.get_sessions(start_date=start, end_date=today, limit=500)
    # 체성분은 항상 전체 이력 (sparse data이므로)
    body = adapter.get_body_metrics(limit=200)
    return sessions, body


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, days: int = 90):
    sessions, body_metrics = _get_all_data(days)
    sessions.sort(key=lambda s: s.date)

    today = date.today()

    # 이번 주
    week_start = today - timedelta(days=today.weekday())
    week_sessions = [s for s in sessions if s.date >= week_start]
    prev_week_start = week_start - timedelta(days=7)
    prev_week_sessions = [s for s in sessions if prev_week_start <= s.date < week_start]

    # 기본 통계
    total_sessions = len(sessions)
    total_volume = sum(s.total_volume for s in sessions)
    workout_count = sum(1 for s in sessions if s.session_type == SessionType.WORKOUT)
    reset_count = sum(1 for s in sessions if s.session_type == SessionType.RESET)

    # 주간 볼륨 추이 (차트용)
    weekly_volumes = defaultdict(float)
    weekly_counts = defaultdict(int)
    for s in sessions:
        wk = s.date - timedelta(days=s.date.weekday())
        weekly_volumes[wk] += s.total_volume
        weekly_counts[wk] += 1
    chart_weeks = sorted(weekly_volumes.keys())
    chart_labels = [w.strftime("%m/%d") for w in chart_weeks]
    chart_volumes = [weekly_volumes[w] for w in chart_weeks]
    chart_counts = [weekly_counts[w] for w in chart_weeks]

    # 운동별 1RM 추이 (상위 5개 운동)
    ex_data: dict[str, list[tuple[str, float]]] = defaultdict(list)
    ex_volumes: dict[str, float] = defaultdict(float)
    for s in sessions:
        for ex in s.exercises:
            ex_volumes[ex.name] += ex.total_volume
            if ex.top_set:
                ex_data[ex.name].append((s.date.isoformat(), ex.top_set.estimated_1rm))

    top_exercises = sorted(ex_volumes.items(), key=lambda x: x[1], reverse=True)[:6]
    exercise_trends = {}
    for name, _ in top_exercises:
        if name in ex_data:
            points = ex_data[name]
            exercise_trends[name] = {
                "dates": [p[0] for p in points],
                "e1rms": [round(p[1], 1) for p in points],
            }

    # 근육군 비율
    group_vols = defaultdict(float)
    for s in sessions:
        for ex in s.exercises:
            g = classify_muscle_group(ex.name)
            group_vols[g] += ex.total_volume
    muscle_labels = list(group_vols.keys())
    muscle_values = [group_vols[g] for g in muscle_labels]
    muscle_label_kr = {"push": "Push", "pull": "Pull", "legs": "Legs", "core": "Core", "other": "기타"}
    muscle_labels = [muscle_label_kr.get(g, g) for g in group_vols.keys()]

    # 체성분 추이 (전체 이력)
    body_sorted = sorted(body_metrics, key=lambda m: m.date)
    body_dates = [m.date.isoformat() for m in body_sorted]
    body_weights = [m.weight_kg for m in body_sorted]
    body_fat = [m.body_fat_pct if m.body_fat_pct else None for m in body_sorted]
    body_muscle = [m.skeletal_muscle_kg if m.skeletal_muscle_kg else None for m in body_sorted]

    # 최근 세션 목록 (역순)
    recent_sessions = sorted(sessions, key=lambda s: (s.date, s.session_number or 0), reverse=True)[:15]

    # 이번 주 인사이트
    weekly_insight = None
    if week_sessions:
        try:
            wi = engine.weekly_insight(week_sessions, prev_week_sessions, sessions, body_metrics or None)
            weekly_insight = wi
        except Exception:
            pass

    # PR 감지 (이번 주)
    prs = []
    if weekly_insight and weekly_insight.prs:
        prs = weekly_insight.prs[:5]

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "today": today,
        "days": days,
        "total_sessions": total_sessions,
        "total_volume": total_volume,
        "workout_count": workout_count,
        "reset_count": reset_count,
        # charts
        "chart_labels": chart_labels,
        "chart_volumes": chart_volumes,
        "chart_counts": chart_counts,
        "exercise_trends": exercise_trends,
        "muscle_labels": muscle_labels,
        "muscle_values": muscle_values,
        "body_dates": body_dates,
        "body_weights": body_weights,
        "body_fat": body_fat,
        "body_muscle": body_muscle,
        # tables
        "recent_sessions": recent_sessions,
        "top_exercises": top_exercises,
        # insight
        "weekly_insight": weekly_insight,
        "prs": prs,
    })


@app.get("/api/sessions")
async def api_sessions(days: int = 90):
    sessions, _ = _get_all_data(days)
    result = []
    for s in sorted(sessions, key=lambda s: s.date, reverse=True):
        exercises = []
        for ex in s.exercises:
            sets = []
            for st in ex.sets:
                sets.append({
                    "weight_kg": st.weight_kg, "reps": st.reps,
                    "duration_sec": st.duration_sec, "side": st.side,
                })
            exercises.append({"name": ex.name, "volume": ex.total_volume, "sets": sets})
        result.append({
            "id": s.id, "date": s.date.isoformat(),
            "label": s.label, "day": s.day_label,
            "type": s.session_type.value,
            "volume": s.total_volume,
            "exercises": exercises,
        })
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
