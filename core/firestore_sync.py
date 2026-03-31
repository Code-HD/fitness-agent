"""Firestore 동기화 모듈.

MCP에서 세션/체성분 기록 시 Firestore에도 자동으로 동기화.
Firebase CLI의 refresh token을 사용하여 REST API로 접근.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Firebase project config
PROJECT_ID = "fitness-eb293"
FIREBASE_CLI_CONFIG = Path.home() / ".config" / "configstore" / "firebase-tools.json"
FIREBASE_CLI_CLIENT_ID = "563584335869-fgrhgmd47bqnekij5i8b5pr03ho849e6.apps.googleusercontent.com"
FIREBASE_CLI_CLIENT_SECRET = "j9iVZfS8kkCEFUPaAeJV0sAi"
FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents"

# User UID (from Firestore)
USER_UID = "V5uRCTRak5ZXAUb6UpNE0PQe2H63"


def _get_access_token() -> str | None:
    """Firebase CLI의 refresh token으로 access token 획득."""
    try:
        if not FIREBASE_CLI_CONFIG.exists():
            logger.warning("Firebase CLI config not found")
            return None

        with open(FIREBASE_CLI_CONFIG) as f:
            config = json.load(f)

        refresh_token = config.get("tokens", {}).get("refresh_token")
        if not refresh_token:
            logger.warning("No refresh token in Firebase CLI config")
            return None

        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": FIREBASE_CLI_CLIENT_ID,
                "client_secret": FIREBASE_CLI_CLIENT_SECRET,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as e:
        logger.error(f"Failed to get access token: {e}")
        return None


def _to_firestore_value(val):
    """Python 값 → Firestore REST API 값 형식 변환."""
    if val is None:
        return {"nullValue": None}
    elif isinstance(val, bool):
        return {"booleanValue": val}
    elif isinstance(val, int):
        return {"integerValue": str(val)}
    elif isinstance(val, float):
        return {"doubleValue": val}
    elif isinstance(val, str):
        return {"stringValue": val}
    elif isinstance(val, list):
        if not val:
            return {"arrayValue": {}}
        return {"arrayValue": {"values": [_to_firestore_value(v) for v in val]}}
    elif isinstance(val, dict):
        return {"mapValue": {"fields": {k: _to_firestore_value(v) for k, v in val.items()}}}
    else:
        return {"stringValue": str(val)}


def _session_to_firestore_doc(session, week: int | None = None) -> dict:
    """Session 객체 → Firestore 문서 필드 변환."""
    from core.models import SessionType

    day_labels = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

    exercises_data = []
    for ex in session.exercises:
        sets_data = []
        for s in ex.sets:
            sets_data.append({
                "weight_kg": s.weight_kg,
                "reps": s.reps,
                "duration_sec": int(s.duration_sec) if s.duration_sec else None,
                "side": s.side,
            })
        exercises_data.append({
            "name": ex.name,
            "sets": sets_data,
            "total_volume": int(ex.total_volume),
        })

    label = session.label if hasattr(session, 'label') else (
        f"오운동-{session.session_number}" if session.session_type == SessionType.WORKOUT else "더리셋"
    )

    fields = {
        "date": str(session.date),
        "session_type": session.session_type.value if hasattr(session.session_type, 'value') else str(session.session_type),
        "session_number": session.session_number,
        "label": label,
        "day_label": day_labels[session.date.weekday()],
        "total_volume": int(session.total_volume),
        "exercises": exercises_data,
        "note": session.note or "",
        "week": week,
    }
    return fields


def _body_to_firestore_doc(metrics) -> dict:
    """BodyMetrics 객체 → Firestore 문서 필드 변환."""
    return {
        "date": str(metrics.date),
        "weight_kg": metrics.weight_kg,
        "body_fat_pct": metrics.body_fat_pct,
        "skeletal_muscle_kg": metrics.skeletal_muscle_kg,
        "bmi": metrics.bmi,
        "body_fat_kg": metrics.body_fat_kg,
        "note": metrics.note or "",
    }


def sync_session(session, week: int | None = None, insight: str = "") -> bool:
    """세션을 Firestore에 동기화."""
    token = _get_access_token()
    if not token:
        logger.warning("Firestore sync skipped: no access token")
        return False

    try:
        doc_data = _session_to_firestore_doc(session, week)
        if insight:
            doc_data["insight"] = insight

        # Firestore document fields
        fields = {k: _to_firestore_value(v) for k, v in doc_data.items()}

        # Create document with auto-generated ID
        url = f"{FIRESTORE_BASE}/users/{USER_UID}/sessions"
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"fields": fields},
            timeout=15,
        )

        if resp.status_code in (200, 201):
            doc_name = resp.json().get("name", "")
            doc_id = doc_name.split("/")[-1] if doc_name else "unknown"
            logger.info(f"Firestore sync OK: session {session.label} → {doc_id}")
            return True
        else:
            logger.error(f"Firestore sync failed: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Firestore sync error: {e}")
        return False


def sync_body_metrics(metrics) -> bool:
    """체성분을 Firestore에 동기화."""
    token = _get_access_token()
    if not token:
        logger.warning("Firestore sync skipped: no access token")
        return False

    try:
        doc_data = _body_to_firestore_doc(metrics)
        fields = {k: _to_firestore_value(v) for k, v in doc_data.items()}

        url = f"{FIRESTORE_BASE}/users/{USER_UID}/body_metrics"
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"fields": fields},
            timeout=15,
        )

        if resp.status_code in (200, 201):
            logger.info(f"Firestore sync OK: body_metrics {metrics.date}")
            return True
        else:
            logger.error(f"Firestore sync failed: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Firestore sync error: {e}")
        return False
