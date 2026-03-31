"""SQLite 스토리지 어댑터 — 세션 기반 통합 구조."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from adapters.base import StorageAdapter
from core.models import (
    BodyMetrics,
    Exercise,
    ExerciseSet,
    Session,
    SessionType,
    WeeklyPlan,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    week_number INTEGER NOT NULL,
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(year, week_number)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER REFERENCES weekly_plans(id),
    date TEXT NOT NULL,
    session_type TEXT NOT NULL,
    session_number INTEGER,
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    exercise_order INTEGER NOT NULL,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS exercise_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
    set_number INTEGER NOT NULL,
    weight_kg REAL,
    reps INTEGER,
    duration_sec REAL,
    side TEXT,
    rpe REAL,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS body_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    body_fat_pct REAL,
    skeletal_muscle_kg REAL,
    bmi REAL,
    body_fat_kg REAL,
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);
CREATE INDEX IF NOT EXISTS idx_sessions_plan ON sessions(plan_id);
CREATE INDEX IF NOT EXISTS idx_exercises_session ON exercises(session_id);
CREATE INDEX IF NOT EXISTS idx_sets_exercise ON exercise_sets(exercise_id);
CREATE INDEX IF NOT EXISTS idx_body_metrics_date ON body_metrics(date);
"""


class SQLiteAdapter(StorageAdapter):
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def _init_db(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add new columns to existing tables if missing."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(body_metrics)").fetchall()}
        for col, typ in [
            ("skeletal_muscle_kg", "REAL"),
            ("bmi", "REAL"),
            ("body_fat_kg", "REAL"),
        ]:
            if col not in cols:
                self.conn.execute(f"ALTER TABLE body_metrics ADD COLUMN {col} {typ}")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- Weekly Plan ---

    def save_plan(self, plan: WeeklyPlan) -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO weekly_plans (year, week_number, note) VALUES (?, ?, ?)",
            (plan.year, plan.week_number, plan.note),
        )
        if cur.lastrowid == 0:
            row = self.conn.execute(
                "SELECT id FROM weekly_plans WHERE year = ? AND week_number = ?",
                (plan.year, plan.week_number),
            ).fetchone()
            plan_id = row["id"]
        else:
            plan_id = cur.lastrowid
        self.conn.commit()
        return plan_id

    def get_plan(self, year: int, week_number: int) -> Optional[WeeklyPlan]:
        row = self.conn.execute(
            "SELECT * FROM weekly_plans WHERE year = ? AND week_number = ?",
            (year, week_number),
        ).fetchone()
        if not row:
            return None
        plan = WeeklyPlan(
            id=row["id"], year=row["year"], week_number=row["week_number"],
            note=row["note"] or "",
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        )
        plan.sessions = self.get_sessions(plan_id=plan.id, limit=20)
        return plan

    def get_plans(self, limit: int = 10) -> list[WeeklyPlan]:
        rows = self.conn.execute(
            "SELECT * FROM weekly_plans ORDER BY year DESC, week_number DESC LIMIT ?",
            (limit,),
        ).fetchall()
        plans = []
        for r in rows:
            p = WeeklyPlan(
                id=r["id"], year=r["year"], week_number=r["week_number"],
                note=r["note"] or "",
            )
            p.sessions = self.get_sessions(plan_id=p.id, limit=20)
            plans.append(p)
        return plans

    # --- Session ---

    def save_session(self, session: Session) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions (plan_id, date, session_type, session_number, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (session.plan_id, session.date.isoformat(), session.session_type.value,
             session.session_number, session.note),
        )
        session_id = cur.lastrowid

        for ex in session.exercises:
            ex_cur = self.conn.execute(
                "INSERT INTO exercises (session_id, name, exercise_order, note) "
                "VALUES (?, ?, ?, ?)",
                (session_id, ex.name, ex.order, ex.note),
            )
            exercise_id = ex_cur.lastrowid

            for s in ex.sets:
                self.conn.execute(
                    "INSERT INTO exercise_sets "
                    "(exercise_id, set_number, weight_kg, reps, duration_sec, side, rpe, note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (exercise_id, s.set_number, s.weight_kg, s.reps,
                     s.duration_sec, s.side, s.rpe, s.note),
                )

        self.conn.commit()
        return session_id

    def _load_exercises(self, session_id: int) -> list[Exercise]:
        ex_rows = self.conn.execute(
            "SELECT * FROM exercises WHERE session_id = ? ORDER BY exercise_order",
            (session_id,),
        ).fetchall()

        exercises = []
        for er in ex_rows:
            ex = Exercise(
                id=er["id"], session_id=session_id,
                name=er["name"], order=er["exercise_order"], note=er["note"] or "",
            )
            set_rows = self.conn.execute(
                "SELECT * FROM exercise_sets WHERE exercise_id = ? ORDER BY set_number",
                (er["id"],),
            ).fetchall()
            for sr in set_rows:
                ex.sets.append(ExerciseSet(
                    id=sr["id"], exercise_id=er["id"],
                    set_number=sr["set_number"],
                    weight_kg=sr["weight_kg"], reps=sr["reps"],
                    duration_sec=sr["duration_sec"], side=sr["side"],
                    rpe=sr["rpe"], note=sr["note"] or "",
                ))
            exercises.append(ex)
        return exercises

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        s = Session(
            id=row["id"], plan_id=row["plan_id"],
            date=date.fromisoformat(row["date"]),
            session_type=SessionType(row["session_type"]),
            session_number=row["session_number"],
            note=row["note"] or "",
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        )
        s.exercises = self._load_exercises(s.id)
        return s

    def get_sessions(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        session_type: Optional[SessionType] = None,
        plan_id: Optional[int] = None,
        limit: int = 100,
    ) -> list[Session]:
        query = "SELECT * FROM sessions WHERE 1=1"
        params: list = []

        if start_date:
            query += " AND date >= ?"
            params.append(start_date.isoformat())
        if end_date:
            query += " AND date <= ?"
            params.append(end_date.isoformat())
        if session_type:
            query += " AND session_type = ?"
            params.append(session_type.value)
        if plan_id:
            query += " AND plan_id = ?"
            params.append(plan_id)

        query += " ORDER BY date DESC, id DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_session(r) for r in rows]

    def get_session_by_id(self, session_id: int) -> Optional[Session]:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row_to_session(row) if row else None

    # --- Body Metrics ---

    def save_body_metrics(self, metrics: BodyMetrics) -> int:
        cur = self.conn.execute(
            "INSERT INTO body_metrics (date, weight_kg, body_fat_pct, skeletal_muscle_kg, bmi, body_fat_kg, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (metrics.date.isoformat(), metrics.weight_kg, metrics.body_fat_pct,
             metrics.skeletal_muscle_kg, metrics.bmi, metrics.body_fat_kg, metrics.note),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_body_metrics(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100,
    ) -> list[BodyMetrics]:
        query = "SELECT * FROM body_metrics WHERE 1=1"
        params: list = []
        if start_date:
            query += " AND date >= ?"
            params.append(start_date.isoformat())
        if end_date:
            query += " AND date <= ?"
            params.append(end_date.isoformat())
        query += " ORDER BY date DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            kwargs = dict(
                id=r["id"], date=date.fromisoformat(r["date"]),
                weight_kg=r["weight_kg"], body_fat_pct=r["body_fat_pct"],
                note=r["note"] or "",
                created_at=datetime.fromisoformat(r["created_at"]) if r["created_at"] else None,
            )
            # Extended fields (may not exist in older DBs)
            try:
                kwargs["skeletal_muscle_kg"] = r["skeletal_muscle_kg"]
                kwargs["bmi"] = r["bmi"]
                kwargs["body_fat_kg"] = r["body_fat_kg"]
            except (IndexError, KeyError):
                pass
            results.append(BodyMetrics(**kwargs))
        return results

    # --- Utility ---

    def get_exercise_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT name FROM exercises ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]
