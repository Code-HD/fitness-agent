"""오운동 시리즈 기록 입력 CLI.

사용법:
  # 주간 계획 생성
  python cli/log.py week --number 9

  # 세션 기록 (오운동-1)
  python cli/log.py session --week 9 --num 1 --date 2026-02-24

  # 세션에 운동 추가
  python cli/log.py add --session 1 --name "랫풀다운(N)" --sets "25kgx13, 35kgx10, 40kgx8"
  python cli/log.py add --session 1 --name "데드행" --sets "42s, 27s, 20s"
  python cli/log.py add --session 1 --name "Pull-up" --sets "1, 1, 1, 1"

  # 더리셋 (필라테스) 세션
  python cli/log.py session --week 9 --type reset --date 2026-02-28

  # 체성분
  python cli/log.py body --weight 75.2 --bodyfat 18.5
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db import get_adapter
from core.models import (
    BodyMetrics,
    Exercise,
    Session,
    SessionType,
    WeeklyPlan,
    parse_sets,
)


def cmd_week(args: argparse.Namespace) -> None:
    """주간 계획 생성."""
    adapter = get_adapter()
    year = args.year or date.today().year
    plan = WeeklyPlan(year=year, week_number=args.number, note=args.note or "")
    plan_id = adapter.save_plan(plan)
    print(f"✓ 오운동 주간Plan - Week {args.number} (ID: {plan_id})")


def cmd_session(args: argparse.Namespace) -> None:
    """세션 생성 (오운동-N 또는 더리셋)."""
    adapter = get_adapter()
    session_date = date.fromisoformat(args.date) if args.date else date.today()
    session_type = SessionType(args.type)

    plan_id = None
    if args.week:
        year = args.year or session_date.year
        plan = adapter.get_plan(year, args.week)
        if not plan:
            plan = WeeklyPlan(year=year, week_number=args.week)
            plan_id = adapter.save_plan(plan)
        else:
            plan_id = plan.id

    session = Session(
        date=session_date,
        session_type=session_type,
        session_number=args.num if session_type == SessionType.WORKOUT else None,
        plan_id=plan_id,
        note=args.note or "",
    )
    session_id = adapter.save_session(session)

    days = ["월", "화", "수", "목", "금", "토", "일"]
    day_label = days[session_date.weekday()]
    label = session.label

    print(f"✓ {label} | {session_date} ({day_label}) (ID: {session_id})")
    if args.week:
        print(f"  Week {args.week}")


def cmd_add(args: argparse.Namespace) -> None:
    """세션에 운동 추가."""
    adapter = get_adapter()

    session = adapter.get_session_by_id(args.session)
    if not session:
        print(f"에러: 세션 ID {args.session}을 찾을 수 없습니다")
        sys.exit(1)

    order = len(session.exercises) + 1
    sets = parse_sets(args.sets, rpe=args.rpe) if args.sets else []

    exercise = Exercise(name=args.name, order=order, sets=sets, note=args.note or "")
    session.exercises = [exercise]

    # 운동 하나만 추가 저장
    conn = adapter.conn
    ex_cur = conn.execute(
        "INSERT INTO exercises (session_id, name, exercise_order, note) VALUES (?, ?, ?, ?)",
        (args.session, exercise.name, exercise.order, exercise.note),
    )
    exercise_id = ex_cur.lastrowid

    for s in exercise.sets:
        conn.execute(
            "INSERT INTO exercise_sets "
            "(exercise_id, set_number, weight_kg, reps, duration_sec, side, rpe, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (exercise_id, s.set_number, s.weight_kg, s.reps,
             s.duration_sec, s.side, s.rpe, s.note),
        )
    conn.commit()

    print(f"✓ [{session.label}] {exercise.name} 추가 (#{order})")
    _print_sets(sets)


def cmd_quick(args: argparse.Namespace) -> None:
    """세션 생성 + 운동들 한번에 기록.

    python cli/log.py quick --week 9 --num 1 --date 2026-02-24 \\
      -e "데드행: 42s, 27s, 20s" \\
      -e "랫풀다운(N): 25kgx13, 35kgx10, 40kgx8" \\
      -e "Pull-up: 1, 1, 1, 1"
    """
    adapter = get_adapter()
    session_date = date.fromisoformat(args.date) if args.date else date.today()
    session_type = SessionType(args.type)

    plan_id = None
    if args.week:
        year = args.year or session_date.year
        plan = adapter.get_plan(year, args.week)
        if not plan:
            plan = WeeklyPlan(year=year, week_number=args.week)
            plan_id = adapter.save_plan(plan)
        else:
            plan_id = plan.id

    exercises = []
    for i, entry in enumerate(args.exercise, start=1):
        if ":" in entry:
            name, sets_str = entry.split(":", 1)
            sets = parse_sets(sets_str.strip())
        else:
            name = entry
            sets = []
        exercises.append(Exercise(name=name.strip(), order=i, sets=sets))

    session = Session(
        date=session_date,
        session_type=session_type,
        session_number=args.num if session_type == SessionType.WORKOUT else None,
        plan_id=plan_id,
        exercises=exercises,
        note=args.note or "",
    )
    session_id = adapter.save_session(session)

    days = ["월", "화", "수", "목", "금", "토", "일"]
    day_label = days[session_date.weekday()]

    print(f"✓ {session.label} | {session_date} ({day_label}) (ID: {session_id})")
    if args.week:
        print(f"  Week {args.week}")
    for ex in exercises:
        print(f"  * {ex.name}")
        _print_sets(ex.sets)
    if session.total_volume > 0:
        print(f"  총 볼륨: {session.total_volume:,.0f} kg")


def cmd_body(args: argparse.Namespace) -> None:
    """체성분 기록."""
    adapter = get_adapter()
    metrics_date = date.fromisoformat(args.date) if args.date else date.today()
    metrics = BodyMetrics(
        date=metrics_date, weight_kg=args.weight,
        body_fat_pct=args.bodyfat, note=args.note or "",
    )
    mid = adapter.save_body_metrics(metrics)
    print(f"✓ 체성분 기록 (ID: {mid})")
    print(f"  {metrics_date} | {args.weight}kg", end="")
    if args.bodyfat:
        print(f" | 체지방 {args.bodyfat}%", end="")
    print()


def _print_sets(sets):
    for s in sets:
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
        if s.rpe:
            parts.append(f"@{s.rpe}")
        if parts:
            print(f"    {s.set_number}. {''.join(parts)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="오운동 시리즈 - 기록")
    sub = parser.add_subparsers(dest="command", required=True)

    # week
    wp = sub.add_parser("week", help="주간 계획 생성")
    wp.add_argument("--number", "-n", type=int, required=True, help="주차 (예: 9)")
    wp.add_argument("--year", "-y", type=int, help="연도 (기본: 올해)")
    wp.add_argument("--note", help="메모")

    # session
    sp = sub.add_parser("session", help="세션 생성")
    sp.add_argument("--week", "-w", type=int, help="주차")
    sp.add_argument("--num", "-n", type=int, help="세션 번호 (오운동-N)")
    sp.add_argument("--type", "-t", choices=["workout", "reset"], default="workout")
    sp.add_argument("--date", "-d", help="날짜 (YYYY-MM-DD)")
    sp.add_argument("--year", "-y", type=int)
    sp.add_argument("--note", help="메모")

    # add (운동 추가)
    ap = sub.add_parser("add", help="세션에 운동 추가")
    ap.add_argument("--session", "-s", type=int, required=True, help="세션 ID")
    ap.add_argument("--name", "-n", required=True, help="운동명")
    ap.add_argument("--sets", help="세트 기록 (예: '100x5, 105x3' 또는 '42s, 27s')")
    ap.add_argument("--rpe", type=float, help="RPE")
    ap.add_argument("--note", help="메모")

    # quick (세션 + 운동 한번에)
    qp = sub.add_parser("quick", help="세션 + 운동 한번에 기록")
    qp.add_argument("--week", "-w", type=int, help="주차")
    qp.add_argument("--num", "-n", type=int, help="세션 번호")
    qp.add_argument("--type", "-t", choices=["workout", "reset"], default="workout")
    qp.add_argument("--date", "-d", help="날짜")
    qp.add_argument("--year", "-y", type=int)
    qp.add_argument("--exercise", "-e", action="append", required=True,
                     help="'운동명: 세트' (반복 가능)")
    qp.add_argument("--note", help="메모")

    # body
    bp = sub.add_parser("body", help="체성분 기록")
    bp.add_argument("--weight", "-w", type=float, required=True)
    bp.add_argument("--bodyfat", "-bf", type=float)
    bp.add_argument("--date", "-d", help="날짜")
    bp.add_argument("--note", help="메모")

    args = parser.parse_args()
    {"week": cmd_week, "session": cmd_session, "add": cmd_add,
     "quick": cmd_quick, "body": cmd_body}[args.command](args)


if __name__ == "__main__":
    main()
