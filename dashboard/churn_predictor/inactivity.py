from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .aimharder import AimHarderClient, AimHarderError
from .config import CenterConfig, ROOT, load_settings, save_center_tokens
from .local_data import LocalSignals, load_signals
from .scoring import _nested

CACHE_PATH = ROOT / "reports" / "inactive_members.json"
THRESHOLD_DAYS = 7
BONO_VALID_MONTHS = 4


def load_inactive_members_cache(path: Path = CACHE_PATH) -> dict:
    if not path.exists():
        return {"generated_at": "", "rows": [], "errors": [], "threshold_days": THRESHOLD_DAYS}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"generated_at": "", "rows": [], "errors": ["No se pudo leer la caché de inactivos"], "threshold_days": THRESHOLD_DAYS}
    data.setdefault("rows", [])
    data.setdefault("errors", [])
    data.setdefault("threshold_days", THRESHOLD_DAYS)
    data.setdefault("generated_at", "")
    return data


def refresh_inactive_members(path: Path = CACHE_PATH) -> dict:
    settings = load_settings()
    signals = load_signals(settings.data_dir)
    rows: List[Dict[str, Any]] = []
    errors: List[str] = []

    cutoff = (date.today() - timedelta(days=THRESHOLD_DAYS)).isoformat()
    for center in settings.centers:
        try:
            clients, fresh_center = AimHarderClient(center).list_clients_no_booking_since(cutoff)
        except AimHarderError as exc:
            errors.append(str(exc))
            continue
        if fresh_center != center:
            save_center_tokens(fresh_center)
        rows.extend(_inactive_rows_for_center(clients, fresh_center, signals))

    rows.sort(key=lambda item: (item.get("center") or "", _sort_days(item), item.get("name") or ""))
    payload = {
        "generated_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "threshold_days": THRESHOLD_DAYS,
        "rows": rows,
        "errors": errors,
        "centers": _center_counts(rows),
        "kpis": _kpis(rows),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _inactive_rows_for_center(clients: List[Dict[str, Any]], center: CenterConfig, signals_index) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for client in clients:
        if client.get("deactivation_date"):
            continue
        name = _full_name(client)
        if not name:
            continue
        last_booking = _nested(client, "class_data", "reservation_date") or ""
        days = _days_since(last_booking)
        if days is not None and days <= THRESHOLD_DAYS:
            continue
        client_id = str(client.get("id") or client.get("Id") or "")
        signal: LocalSignals = signals_index.find(
            client_id=client_id,
            email=str(client.get("email") or ""),
            phone=str(client.get("mobile_number") or client.get("mobile") or ""),
            name=name,
        )
        if _excluded_from_inactivity(signal):
            continue
        if _expired_class_pack(signal):
            continue
        out.append({
            "id": client_id,
            "name": name,
            "center": center.name,
            "phone": str(client.get("mobile_number") or client.get("mobile") or ""),
            "email": str(client.get("email") or ""),
            "created_at": str(client.get("creation_date") or ""),
            "last_class_at": last_booking,
            "days_without_class": days,
            "bucket": _bucket(days),
            "membership_name": signal.membership_name,
            "last_membership_payment_date": signal.last_membership_payment_date.isoformat() if signal.last_membership_payment_date else "",
            "weekly_average": signal.weekly_average,
            "membership_active": _membership_active(signal),
            "source": "AimHarder /clients/no-booking",
        })
    return out


def _kpis(rows: List[Dict[str, Any]]) -> dict:
    return {
        "listed": len(rows),
        "days_8_14": len([r for r in rows if _days_in_range(r, 8, 14)]),
        "days_15_21": len([r for r in rows if _days_in_range(r, 15, 21)]),
        "days_22_30": len([r for r in rows if _days_in_range(r, 22, 30)]),
        "days_31_plus": len([r for r in rows if isinstance(r.get("days_without_class"), int) and r["days_without_class"] >= 31]),
        "no_booking": len([r for r in rows if r.get("days_without_class") is None]),
    }


def _center_counts(rows: List[Dict[str, Any]]) -> dict:
    counts: Dict[str, int] = {}
    for row in rows:
        center = str(row.get("center") or "Sin centro")
        counts[center] = counts.get(center, 0) + 1
    return counts


def _days_in_range(row: Dict[str, Any], low: int, high: int) -> bool:
    days = row.get("days_without_class")
    return isinstance(days, int) and low <= days <= high


def _sort_days(item: Dict[str, Any]) -> int:
    days = item.get("days_without_class")
    if days is None:
        return 9999
    try:
        return -int(days)
    except Exception:
        return 9999


def _membership_active(signal: LocalSignals) -> bool:
    if not signal.last_membership_payment_date:
        return False
    if _is_class_pack(signal.membership_name):
        return date.today() <= _add_months(signal.last_membership_payment_date, BONO_VALID_MONTHS)
    return (date.today() - signal.last_membership_payment_date).days <= 31


def _excluded_from_inactivity(signal: LocalSignals) -> bool:
    return _is_wellhub(signal.membership_name)


def _expired_class_pack(signal: LocalSignals) -> bool:
    if not _is_class_pack(signal.membership_name):
        return False
    if not signal.last_membership_payment_date:
        return False
    return date.today() > _add_months(signal.last_membership_payment_date, BONO_VALID_MONTHS)


def _is_class_pack(membership_name: str) -> bool:
    normalized = " ".join(str(membership_name or "").lower().split())
    return normalized in {"bono 10 clases", "bono 10 clases crossfit academy"}


def _is_wellhub(membership_name: str) -> bool:
    normalized = " ".join(str(membership_name or "").lower().split())
    return "wellhub" in normalized or "gympass" in normalized


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(value.day, days_in_month[month - 1]))


def _bucket(days: Optional[int]) -> str:
    if days is None:
        return "Sin registro"
    if days >= 31:
        return "31+ días"
    if days >= 22:
        return "22-30 días"
    if days >= 15:
        return "15-21 días"
    return "8-14 días"


def _days_since(value: str) -> Optional[int]:
    parsed = _parse_date(value)
    if not parsed:
        return None
    return (date.today() - parsed).days


def _parse_date(value: str) -> Optional[date]:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw[:19]).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%y"):
        try:
            return datetime.strptime(raw[:19], fmt).date()
        except ValueError:
            continue
    return None


def _full_name(client: Dict[str, Any]) -> str:
    return " ".join(
        str(client.get(key) or "").strip()
        for key in ("name", "first_surname", "second_surname")
        if str(client.get(key) or "").strip()
    )
