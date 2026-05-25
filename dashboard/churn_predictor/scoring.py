from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Optional

from .local_data import LocalSignals


@dataclass
class ScoredClient:
    client_id: str
    name: str
    center: str
    score: int
    risk: str
    reasons: str
    phone: str
    email: str
    created_at: str
    last_booking_at: str
    days_without_class: Optional[int]
    membership_name: str
    weekly_average: Optional[float]
    last_payment_date: str
    last_membership_payment_date: str
    failed_payments_120d: int
    cancellation_requested: bool


def score_client(client: Dict[str, Any], center: str, signal: LocalSignals, high: int, medium: int) -> ScoredClient:
    today = date.today()
    last_booking = _nested(client, "class_data", "reservation_date") or ""
    created = str(client.get("creation_date") or "")
    days_without_class = _days_since(last_booking)
    age_months = _months_since(created)
    score = 0
    reasons = []
    real_churn = False

    if signal.last_membership_payment_date:
        days_since_membership_payment = (today - signal.last_membership_payment_date).days
        if days_since_membership_payment > 31:
            real_churn = True
            score = 100
            reasons.append(
                f"tarifa vencida hace {days_since_membership_payment - 31} dias sin pago posterior"
            )

    if signal.cancellation_requested and not real_churn:
        score += 45
        reasons.append("cancelacion solicitada")

    if days_without_class is None and not real_churn:
        score += 28
        reasons.append("sin reserva registrada")
    elif days_without_class is not None and days_without_class > 30 and not real_churn:
        score += 40
        reasons.append(f"{days_without_class} dias sin clase")
    elif days_without_class is not None and days_without_class > 21 and not real_churn:
        score += 32
        reasons.append(f"{days_without_class} dias sin clase")
    elif days_without_class is not None and days_without_class > 14 and not real_churn:
        score += 22
        reasons.append(f"{days_without_class} dias sin clase")
    elif days_without_class is not None and days_without_class > 7 and not real_churn:
        score += 10
        reasons.append(f"{days_without_class} dias sin clase")

    if age_months is not None and not real_churn:
        if age_months <= 1:
            score += 18
            reasons.append("alta reciente")
        elif age_months <= 3:
            score += 15
            reasons.append("primeros 3 meses")
        elif age_months <= 6:
            score += 8
            reasons.append("primeros 6 meses")

    if signal.weekly_average is not None and not real_churn:
        if signal.weekly_average < 0.5:
            score += 18
            reasons.append("media semanal muy baja")
        elif signal.weekly_average < 1:
            score += 10
            reasons.append("media semanal baja")

    if signal.failed_payments_120d and not real_churn:
        points = min(signal.failed_payments_120d * 10, 25)
        score += points
        reasons.append(f"{signal.failed_payments_120d} pagos fallidos")

    if signal.last_payment_date and not real_churn and not signal.last_membership_payment_date:
        days_since_payment = (today - signal.last_payment_date).days
        if days_since_payment > 75:
            score += 25
            reasons.append("ultimo pago hace mas de 75 dias")
        elif days_since_payment > 45:
            score += 18
            reasons.append("ultimo pago hace mas de 45 dias")

    membership_points = membership_risk(signal.membership_name)
    if membership_points:
        if not real_churn:
            score += membership_points
        reasons.append(f"tarifa {signal.membership_name}")

    score = min(score, 100)
    risk = "Baja real" if real_churn else "Alto" if score >= high else "Medio" if score >= medium else "Bajo"

    return ScoredClient(
        client_id=str(client.get("id") or client.get("Id") or ""),
        name=_full_name(client),
        center=center,
        score=score,
        risk=risk,
        reasons=", ".join(reasons) or "sin señales fuertes",
        phone=str(client.get("mobile_number") or client.get("mobile") or ""),
        email=str(client.get("email") or ""),
        created_at=created,
        last_booking_at=last_booking,
        days_without_class=days_without_class,
        membership_name=signal.membership_name,
        weekly_average=signal.weekly_average,
        last_payment_date=signal.last_payment_date.isoformat() if signal.last_payment_date else "",
        last_membership_payment_date=(
            signal.last_membership_payment_date.isoformat() if signal.last_membership_payment_date else ""
        ),
        failed_payments_120d=signal.failed_payments_120d,
        cancellation_requested=signal.cancellation_requested,
    )


def membership_risk(name: str) -> int:
    value = name.lower().strip()
    if not value:
        return 0
    if "academy 2d" in value:
        return 20
    if "academy" in value:
        return 15
    if value in {"s", "tarifa s"} or "9 clases" in value:
        return 12
    if value in {"m", "tarifa m"} or "13 clases" in value:
        return 6
    if value in {"l", "xl", "tarifa l", "tarifa xl"}:
        return 0
    return 3


def _full_name(client: Dict[str, Any]) -> str:
    return " ".join(
        str(client.get(key) or "").strip()
        for key in ("name", "first_surname", "second_surname")
        if str(client.get(key) or "").strip()
    )


def _nested(data: Dict[str, Any], *keys: str) -> str:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "")


def _days_since(value: str) -> Optional[int]:
    parsed = _parse_date(value)
    if not parsed:
        return None
    return (date.today() - parsed).days


def _months_since(value: str) -> Optional[int]:
    parsed = _parse_date(value)
    if not parsed:
        return None
    return int((date.today() - parsed).days / 30.44)


def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt).date()
        except ValueError:
            pass
    return None
