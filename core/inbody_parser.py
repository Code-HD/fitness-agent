"""InBody 스크린샷 파서 — Claude Vision으로 체성분 데이터 추출.

InBody 앱 또는 결과지 스크린샷에서 체성분 데이터를 자동 추출.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional


@dataclass
class InBodyResult:
    """InBody 파싱 결과."""
    weight_kg: float
    skeletal_muscle_kg: Optional[float] = None
    body_fat_pct: Optional[float] = None
    body_fat_kg: Optional[float] = None
    bmi: Optional[float] = None
    measurement_date: Optional[date] = None
    raw_text: str = ""


PARSE_PROMPT = """\
이 이미지는 InBody 체성분 분석 결과입니다.
다음 정보를 JSON 형식으로 정확히 추출해주세요:

{
  "weight_kg": 체중(kg),
  "skeletal_muscle_kg": 골격근량(kg),
  "body_fat_pct": 체지방률(%),
  "body_fat_kg": 체지방량(kg),
  "bmi": BMI,
  "date": "YYYY-MM-DD" (날짜가 보이면)
}

- 숫자만 추출, 단위 제외
- 보이지 않는 항목은 null
- JSON만 출력, 다른 텍스트 없이
"""


def parse_inbody_screenshot(
    image_path: str | Path | None = None,
    image_base64: str | None = None,
    model: str = "claude-sonnet-4-20250514",
) -> Optional[InBodyResult]:
    """InBody 스크린샷에서 체성분 데이터 추출.

    Args:
        image_path: 이미지 파일 경로
        image_base64: base64 인코딩된 이미지
        model: Claude 모델

    Returns:
        InBodyResult or None
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    # 이미지 준비
    if image_path:
        path = Path(image_path)
        if not path.exists():
            return None
        with open(path, "rb") as f:
            img_data = base64.standard_b64encode(f.read()).decode()
        suffix = path.suffix.lower()
        media_type = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(suffix, "image/jpeg")
    elif image_base64:
        img_data = image_base64
        media_type = "image/jpeg"
    else:
        return None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": img_data,
                        },
                    },
                    {"type": "text", "text": PARSE_PROMPT},
                ],
            }],
        )

        raw = resp.content[0].text
        # JSON 추출
        json_match = re.search(r"\{[^}]+\}", raw, re.DOTALL)
        if not json_match:
            return None

        data = json.loads(json_match.group())

        weight = data.get("weight_kg")
        if not weight:
            return None

        measurement_date = None
        if data.get("date"):
            try:
                measurement_date = date.fromisoformat(data["date"])
            except (ValueError, TypeError):
                pass

        return InBodyResult(
            weight_kg=float(weight),
            skeletal_muscle_kg=_safe_float(data.get("skeletal_muscle_kg")),
            body_fat_pct=_safe_float(data.get("body_fat_pct")),
            body_fat_kg=_safe_float(data.get("body_fat_kg")),
            bmi=_safe_float(data.get("bmi")),
            measurement_date=measurement_date,
            raw_text=raw,
        )
    except Exception:
        return None


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
