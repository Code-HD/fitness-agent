"""오운동 시리즈 리포트 — 주간/세션/진행 현황."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db import get_adapter
from core.models import Session, SessionType
from core.insight_engine import InsightEngine, render_weekly_insight


def _fmt_set(s) -> str:
    parts = []
    if s.weight_kg is not None:
        parts.append(f"{s.weight_kg}kg")
    if s.reps is not None:
        parts.append(f"×{s.reps}")
    if s.duration_sec is not None:
        if s.duration_sec >= 60:
            parts.append(f"{s.duration_sec / 60:.0f}min")
        else:
            parts.append(f"{s.duration_sec:.0f}s")
    if s.side:
        parts.append(f"({s.side})")
    return "".join(parts) or s.note


def print_session(session: Session) -> None:
    """세션 하나를 오운동 양식으로 출력."""
    print(f"# {session.label} | {session.date} ({session.day_label})")
    if session.plan_id:
        print(f"  (plan ID: {session.plan_id})")
    for ex in session.exercises:
        sets_str = " / ".join(_fmt_set(s) for s in ex.sets)
        line = f"  * {ex.name}"
        if sets_str:
            line += f" — {sets_str}"
        if ex.is_weighted and ex.total_volume > 0:
            line += f"  [{ex.total_volume:,.0f}kg]"
        print(line)
    if session.total_volume > 0:
        print(f"  총 볼륨: {session.total_volume:,.0f} kg")
    print()


def weekly_report(week: int | None = None, year: int | None = None, target_date: date | None = None) -> None:
    adapter = get_adapter()

    if week:
        yr = year or date.today().year
        plan = adapter.get_plan(yr, week)
        if plan:
            print(f"═══ {plan.label} ═══\n")
            sessions = plan.sessions
        else:
            print(f"Week {week} 계획이 없습니다.")
            return
    else:
        today = target_date or date.today()
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        sessions = adapter.get_sessions(start_date=start, end_date=end, limit=20)
        print(f"═══ 주간 리포트 ({start} ~ {end}) ═══\n")

    if not sessions:
        print("이번 주 기록이 없습니다.")
        return

    body = adapter.get_body_metrics(limit=5)

    sessions_sorted = sorted(sessions, key=lambda s: (s.date, s.session_number or 99))

    workout_count = sum(1 for s in sessions_sorted if s.session_type == SessionType.WORKOUT)
    reset_count = sum(1 for s in sessions_sorted if s.session_type == SessionType.RESET)
    total_vol = sum(s.total_volume for s in sessions_sorted)

    print(f"오운동 {workout_count}회", end="")
    if reset_count:
        print(f" + 더리셋 {reset_count}회", end="")
    print(f" | 총 볼륨: {total_vol:,.0f} kg\n")

    for s in sessions_sorted:
        print_session(s)

    if body:
        latest = body[0]
        print(f"⚖️ 체성분 ({latest.date}): {latest.weight_kg}kg", end="")
        if latest.body_fat_pct:
            print(f" | 체지방 {latest.body_fat_pct}%", end="")
        print()

    # 주간 인사이트
    try:
        engine = InsightEngine()
        week_start = sessions_sorted[0].date - timedelta(days=sessions_sorted[0].date.weekday())
        prev_start = week_start - timedelta(days=7)
        prev_end = week_start - timedelta(days=1)
        prev_sessions = adapter.get_sessions(start_date=prev_start, end_date=prev_end, limit=20)
        all_start = week_start - timedelta(days=90)
        all_history = adapter.get_sessions(start_date=all_start, end_date=sessions_sorted[-1].date, limit=500)
        wi = engine.weekly_insight(sessions_sorted, prev_sessions, all_history, body or None)
        print()
        print(render_weekly_insight(wi))
    except Exception:
        pass


def progress_report(exercise: str, days: int = 30) -> None:
    adapter = get_adapter()
    today = date.today()
    start = today - timedelta(days=days)

    sessions = adapter.get_sessions(start_date=start, end_date=today, limit=500)

    print(f"═══ {exercise} 진행 현황 (최근 {days}일) ═══\n")

    found = []
    for s in sessions:
        for ex in s.exercises:
            if ex.name.lower() == exercise.lower():
                found.append((s, ex))

    if not found:
        print(f"'{exercise}' 기록이 없습니다.")
        return

    found.sort(key=lambda x: x[0].date)

    for s, ex in found:
        sets_str = " / ".join(_fmt_set(st) for st in ex.sets)
        line = f"  {s.date} ({s.day_label}) [{s.label}]"
        if sets_str:
            line += f"  {sets_str}"
        if ex.total_volume > 0:
            line += f"  볼륨:{ex.total_volume:,.0f}kg"
        ts = ex.top_set
        if ts:
            line += f"  1RM:{ts.estimated_1rm:.1f}"
        print(line)

    # 1RM 추이
    weighted = [(s, ex) for s, ex in found if ex.top_set]
    if len(weighted) >= 2:
        first = weighted[0][1].top_set.estimated_1rm
        last = weighted[-1][1].top_set.estimated_1rm
        change = last - first
        pct = (change / first * 100) if first > 0 else 0
        sign = "+" if change > 0 else ""
        print(f"\n  추정 1RM: {first:.1f} → {last:.1f} ({sign}{pct:.1f}%)")


def list_exercises() -> None:
    adapter = get_adapter()
    names = adapter.get_exercise_names()
    if not names:
        print("기록된 운동이 없습니다.")
        return
    print("기록된 운동 목록:")
    for n in names:
        print(f"  - {n}")


def main() -> None:
    parser = argparse.ArgumentParser(description="오운동 시리즈 - 리포트")
    parser.add_argument("--type", "-t", choices=["weekly", "progress", "exercises"],
                        default="weekly")
    parser.add_argument("--week", "-w", type=int, help="주차")
    parser.add_argument("--year", "-y", type=int, help="연도")
    parser.add_argument("--exercise", "-e", help="운동명")
    parser.add_argument("--days", "-d", type=int, default=30, help="조회 기간(일)")
    parser.add_argument("--date", help="기준 날짜")

    args = parser.parse_args()

    if args.type == "weekly":
        target = date.fromisoformat(args.date) if args.date else None
        weekly_report(week=args.week, year=args.year, target_date=target)
    elif args.type == "progress":
        if not args.exercise:
            print("에러: --exercise 필요")
            sys.exit(1)
        progress_report(args.exercise, args.days)
    elif args.type == "exercises":
        list_exercises()


if __name__ == "__main__":
    main()
