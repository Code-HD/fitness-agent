"""영양 코치 비즈니스 로직 — 매크로 추정, 일일 집계, EA 계산, 목표 대비 분석."""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Optional

from core.models import (
    DailyNutrition,
    Meal,
    NutritionTarget,
)


# --- 매크로 추정 (Claude Vision / 텍스트) ---

MACRO_ESTIMATION_PROMPT = """\
다음 음식 설명을 읽고, 영양 성분을 추정해 JSON으로 반환하세요.

음식 설명: {description}

반드시 아래 형식의 JSON만 출력하세요. 다른 텍스트 없이:
{{
  "calories": 숫자(kcal),
  "protein_g": 숫자,
  "carbs_g": 숫자,
  "fat_g": 숫자,
  "fiber_g": 숫자,
  "is_natural": true/false
}}

추정 기준:
- 일반적인 한국 식당 기준 1인분 양
- 정확하지 않더라도 합리적 추정
- is_natural: 가공식품/패스트푸드/보충제 = false, 자연식 = true
"""

MEAL_PHOTO_PROMPT = """\
이 사진의 음식을 분석하고, 영양 성분을 추정해 JSON으로 반환하세요.

반드시 아래 형식의 JSON만 출력하세요. 다른 텍스트 없이:
{{
  "description": "음식 설명 (한국어)",
  "calories": 숫자(kcal),
  "protein_g": 숫자,
  "carbs_g": 숫자,
  "fat_g": 숫자,
  "fiber_g": 숫자,
  "is_natural": true/false
}}
"""


def estimate_macros(description: str, model: str = "claude-sonnet-4-20250514") -> dict:
    """자연어 음식 설명에서 매크로 추정 (Claude API)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _fallback_estimate(description)

    try:
        import anthropic
        import json

        client = anthropic.Anthropic(api_key=api_key)
        prompt = MACRO_ESTIMATION_PROMPT.format(description=description)

        resp = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()

        # JSON 추출 (```json ... ``` 형태도 처리)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        return json.loads(text)
    except Exception:
        return _fallback_estimate(description)


def estimate_macros_from_photo(
    image_path: str,
    model: str = "claude-sonnet-4-20250514",
) -> dict:
    """음식 사진에서 매크로 추정 (Claude Vision API)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}

    try:
        import anthropic
        import base64
        import json
        from pathlib import Path

        path = Path(image_path)
        if not path.exists():
            return {"error": f"File not found: {image_path}"}

        suffix = path.suffix.lower()
        media_types = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        }
        media_type = media_types.get(suffix, "image/jpeg")

        image_data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": MEAL_PHOTO_PROMPT},
                ],
            }],
        )
        text = resp.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        return json.loads(text)
    except Exception as e:
        return {"error": str(e)}


def _fallback_estimate(description: str) -> dict:
    """AI 없이 기본 추정 (대략적)."""
    # 키워드 기반 초간단 추정
    desc = description.lower()
    cal, p, c, f, fiber = 400, 20, 50, 10, 3

    if any(k in desc for k in ["닭가슴살", "chicken breast"]):
        cal, p, c, f = 250, 40, 5, 5
    elif any(k in desc for k in ["소고기", "beef", "스테이크"]):
        cal, p, c, f = 350, 35, 0, 20
    elif any(k in desc for k in ["계란", "egg", "달걀"]):
        cal, p, c, f = 150, 12, 1, 10
    elif any(k in desc for k in ["밥", "rice", "현미"]):
        cal, p, c, f = 300, 5, 65, 1
    elif any(k in desc for k in ["샐러드", "salad"]):
        cal, p, c, f, fiber = 150, 5, 15, 8, 8
    elif any(k in desc for k in ["라면", "ramen", "피자", "pizza", "햄버거", "burger"]):
        cal, p, c, f = 600, 15, 70, 25

    return {
        "calories": cal,
        "protein_g": p,
        "carbs_g": c,
        "fat_g": f,
        "fiber_g": fiber,
        "is_natural": not any(
            k in desc for k in ["라면", "피자", "햄버거", "프로틴바", "보충제"]
        ),
    }


# --- 일일 영양 집계 ---


def aggregate_daily_nutrition(
    meals: list[Meal],
    target_date: str,
    training_day: bool = False,
) -> DailyNutrition:
    """해당 날짜 식사 목록에서 일일 영양 집계."""
    day_meals = [m for m in meals if m.date == target_date]

    total_cal = sum(m.calories or 0 for m in day_meals)
    total_p = sum(m.protein_g or 0 for m in day_meals)
    total_c = sum(m.carbs_g or 0 for m in day_meals)
    total_f = sum(m.fat_g or 0 for m in day_meals)
    total_fiber = sum(m.fiber_g or 0 for m in day_meals)
    total_water = sum(m.water_ml or 0 for m in day_meals)

    natural_count = sum(1 for m in day_meals if m.is_natural)
    total_count = len(day_meals)
    natural_ratio = natural_count / total_count if total_count > 0 else 1.0

    return DailyNutrition(
        date=target_date,
        total_calories=total_cal,
        total_protein_g=total_p,
        total_carbs_g=total_c,
        total_fat_g=total_f,
        total_fiber_g=total_fiber,
        total_water_ml=total_water,
        meal_count=total_count,
        natural_ratio=natural_ratio,
        training_day=training_day,
    )


# --- Energy Availability 계산 ---


def calc_energy_availability(
    calorie_intake: float,
    exercise_expenditure: float,
    lean_body_mass_kg: float,
) -> dict:
    """Energy Availability (EA) 계산.

    EA = (Calorie intake - Exercise expenditure) / Lean Body Mass
    - EA < 30 kcal/kg → recovery suppression warning
    - EA < 20 kcal/kg → high risk (hormonal disruption)
    """
    if lean_body_mass_kg <= 0:
        return {"ea": 0, "status": "unknown", "warning": "LBM 데이터 없음"}

    ea = (calorie_intake - exercise_expenditure) / lean_body_mass_kg

    if ea < 20:
        status = "critical"
        warning = f"EA {ea:.1f} kcal/kg — 고위험: 호르몬 교란 가능. 칼로리 즉시 증가 필요"
    elif ea < 30:
        status = "warning"
        warning = f"EA {ea:.1f} kcal/kg — 경고: 회복 억제 가능. 칼로리 확인 필요"
    elif ea < 45:
        status = "optimal"
        warning = None
    else:
        status = "surplus"
        warning = None

    return {"ea": round(ea, 1), "status": status, "warning": warning}


# --- 목표 대비 분석 ---


def analyze_vs_target(
    daily: DailyNutrition,
    target: NutritionTarget,
    weight_kg: float,
) -> dict:
    """일일 섭취량 vs 목표 대비 분석."""
    protein_target = target.protein_g_per_kg * weight_kg
    carbs_target = target.carbs_g_per_kg * weight_kg
    fat_target = target.fat_g_per_kg * weight_kg

    def pct(actual, goal):
        return round(actual / goal * 100, 1) if goal > 0 else 0

    analysis = {
        "calories": {
            "actual": round(daily.total_calories, 0),
            "target": round(target.calories_target, 0),
            "pct": pct(daily.total_calories, target.calories_target),
        },
        "protein_g": {
            "actual": round(daily.total_protein_g, 1),
            "target": round(protein_target, 1),
            "pct": pct(daily.total_protein_g, protein_target),
            "per_kg": round(daily.total_protein_g / weight_kg, 2) if weight_kg > 0 else 0,
        },
        "carbs_g": {
            "actual": round(daily.total_carbs_g, 1),
            "target": round(carbs_target, 1),
            "pct": pct(daily.total_carbs_g, carbs_target),
        },
        "fat_g": {
            "actual": round(daily.total_fat_g, 1),
            "target": round(fat_target, 1),
            "pct": pct(daily.total_fat_g, fat_target),
        },
        "water_ml": {
            "actual": round(daily.total_water_ml, 0),
            "target": round(target.water_ml_target, 0),
            "pct": pct(daily.total_water_ml, target.water_ml_target),
        },
        "fiber_g": daily.total_fiber_g,
        "natural_ratio": round(daily.natural_ratio * 100, 0),
        "meal_count": daily.meal_count,
    }

    # 리스크 시그널
    risks = []
    if analysis["protein_g"]["pct"] < 80:
        risks.append(f"단백질 부족: 목표 대비 {analysis['protein_g']['pct']}%. 즉시 보충 필요")
    if analysis["calories"]["pct"] < 70:
        risks.append(f"칼로리 심각 부족: 목표 대비 {analysis['calories']['pct']}%")
    if analysis["calories"]["pct"] > 130:
        risks.append(f"칼로리 과잉: 목표 대비 {analysis['calories']['pct']}%")
    if daily.total_water_ml < target.water_ml_target * 0.5 and daily.total_water_ml > 0:
        risks.append("수분 심각 부족: 목표의 50% 미만")

    analysis["risks"] = risks

    # 남은 매크로 (오늘 더 먹어야 할 양)
    remaining = {
        "calories": max(0, target.calories_target - daily.total_calories),
        "protein_g": max(0, protein_target - daily.total_protein_g),
        "carbs_g": max(0, carbs_target - daily.total_carbs_g),
        "fat_g": max(0, fat_target - daily.total_fat_g),
        "water_ml": max(0, target.water_ml_target - daily.total_water_ml),
    }
    analysis["remaining"] = remaining

    return analysis


# --- 트렌드 분석 (3-7일) ---


def analyze_nutrition_trend(
    daily_summaries: list[DailyNutrition],
    target: Optional[NutritionTarget] = None,
    weight_kg: float = 88.0,
) -> dict:
    """3-7일 영양 트렌드 분석."""
    if len(daily_summaries) < 3:
        return {"status": "insufficient_data", "message": "최소 3일 데이터 필요"}

    avg_cal = sum(d.total_calories for d in daily_summaries) / len(daily_summaries)
    avg_protein = sum(d.total_protein_g for d in daily_summaries) / len(daily_summaries)
    avg_carbs = sum(d.total_carbs_g for d in daily_summaries) / len(daily_summaries)
    avg_fat = sum(d.total_fat_g for d in daily_summaries) / len(daily_summaries)
    avg_water = sum(d.total_water_ml for d in daily_summaries) / len(daily_summaries)
    avg_natural = sum(d.natural_ratio for d in daily_summaries) / len(daily_summaries)

    trend = {
        "period_days": len(daily_summaries),
        "avg_calories": round(avg_cal, 0),
        "avg_protein_g": round(avg_protein, 1),
        "avg_protein_per_kg": round(avg_protein / weight_kg, 2) if weight_kg > 0 else 0,
        "avg_carbs_g": round(avg_carbs, 1),
        "avg_fat_g": round(avg_fat, 1),
        "avg_water_ml": round(avg_water, 0),
        "avg_natural_ratio": round(avg_natural * 100, 0),
    }

    # 트렌드에서 리스크 감지
    risks = []
    protein_per_kg = avg_protein / weight_kg if weight_kg > 0 else 0
    if protein_per_kg < 1.6:
        risks.append(
            f"단백질 {trend['avg_protein_per_kg']}g/kg — 최소 1.6g/kg 미달. "
            f"일 {round(1.6 * weight_kg - avg_protein, 0)}g 추가 필요"
        )

    fat_per_kg = avg_fat / weight_kg if weight_kg > 0 else 0
    if fat_per_kg < 0.8:
        risks.append(
            f"지방 {fat_per_kg:.2f}g/kg — 최소 0.8g/kg 미달. "
            "호르몬 생성에 영향 가능"
        )

    if target:
        cal_pct = avg_cal / target.calories_target * 100 if target.calories_target > 0 else 0
        if cal_pct < 80:
            risks.append(f"칼로리 평균 {cal_pct:.0f}% — 목표 대비 지속적 부족")
        elif cal_pct > 120:
            risks.append(f"칼로리 평균 {cal_pct:.0f}% — 목표 대비 지속적 과잉")

    trend["risks"] = risks

    return trend


# --- 출력 렌더링 ---


def render_daily_summary(
    daily: DailyNutrition,
    analysis: Optional[dict] = None,
) -> str:
    """일일 영양 요약 텍스트 렌더링."""
    lines = [f"## 일일 영양 요약 ({daily.date})"]
    lines.append(f"식사 횟수: {daily.meal_count}끼")
    lines.append(f"칼로리: {daily.total_calories:,.0f} kcal")
    lines.append(f"단백질: {daily.total_protein_g:.1f}g")
    lines.append(f"탄수화물: {daily.total_carbs_g:.1f}g")
    lines.append(f"지방: {daily.total_fat_g:.1f}g")
    lines.append(f"식이섬유: {daily.total_fiber_g:.1f}g")
    lines.append(f"수분: {daily.total_water_ml:,.0f}ml")
    lines.append(f"자연식 비율: {daily.natural_ratio * 100:.0f}%")

    if analysis:
        lines.append("")
        lines.append("### 목표 대비")
        for key in ["calories", "protein_g", "carbs_g", "fat_g", "water_ml"]:
            if key in analysis and isinstance(analysis[key], dict):
                a = analysis[key]
                lines.append(f"  {key}: {a['actual']} / {a['target']} ({a['pct']}%)")

        if analysis.get("risks"):
            lines.append("")
            lines.append("### ⚠ 리스크 시그널")
            for risk in analysis["risks"]:
                lines.append(f"  - {risk}")

        if analysis.get("remaining"):
            rem = analysis["remaining"]
            lines.append("")
            lines.append("### 남은 매크로")
            if rem["calories"] > 0:
                lines.append(f"  칼로리: {rem['calories']:,.0f} kcal")
            if rem["protein_g"] > 0:
                lines.append(f"  단백질: {rem['protein_g']:.1f}g")
            if rem["carbs_g"] > 0:
                lines.append(f"  탄수화물: {rem['carbs_g']:.1f}g")

    return "\n".join(lines)
