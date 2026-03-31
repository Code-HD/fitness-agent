"""AI 인사이트 생성 CLI."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ai import generate_insight
from core.analyzer import Analyzer
from core.db import get_adapter


def parse_period(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("d"):
        return int(s[:-1])
    elif s.endswith("w"):
        return int(s[:-1]) * 7
    elif s.endswith("m"):
        return int(s[:-1]) * 30
    return int(s)


def main() -> None:
    parser = argparse.ArgumentParser(description="오운동 시리즈 - AI 인사이트")
    parser.add_argument("--period", "-p", default="30d")
    parser.add_argument("--exercise", "-e", help="특정 운동 분석")
    parser.add_argument("--question", "-q", help="추가 질문")
    parser.add_argument("--model", "-m", default="claude-sonnet-4-20250514")

    args = parser.parse_args()
    days = parse_period(args.period)
    adapter = get_adapter()
    today = date.today()
    start = today - timedelta(days=days)

    sessions = adapter.get_sessions(start_date=start, end_date=today, limit=1000)
    body = adapter.get_body_metrics(start_date=start, end_date=today, limit=100)

    if not sessions and not body:
        print(f"최근 {days}일간 기록이 없습니다.")
        return

    analyzer = Analyzer()
    result = analyzer.analyze(sessions, body, period_days=days)

    print(f"═══ 인사이트 (최근 {days}일) ═══\n")
    print(generate_insight(result, user_question=args.question, model=args.model))


if __name__ == "__main__":
    main()
