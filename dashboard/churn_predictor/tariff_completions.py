from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .aimharder import AimHarderClient
from .config import ROOT, load_settings
from .local_data import load_signals

CACHE_PATH = ROOT / "reports" / "tarifas_completadas_cache.json"
CENTER_NAME = "Parla"


def load_tariff_completions_cache() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return {
            "ok": False,
            "center": CENTER_NAME,
            "generated_at": "",
            "rows": [],
            "errors": ["Pendiente de generar el listado."],
            "history_available": False,
        }
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "center": CENTER_NAME,
            "generated_at": "",
            "rows": [],
            "errors": ["Cache de tarifas completadas no válido."],
            "history_available": False,
        }


def refresh_tariff_completions() -> dict[str, Any]:
    settings = load_settings()
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    center = next((item for item in settings.centers if item.name == CENTER_NAME), None)
    if center is None:
        payload = _payload([], [f"Centro {CENTER_NAME} no configurado."], False)
        _write(payload)
        return payload

    signals = load_signals(settings.data_dir)
    errors = [
        "AimHarder no expone todavía en la API pública usada aquí el histórico de asistencias por socio/ciclo. El listado queda preparado con consumo pendiente."
    ]
    rows: list[dict[str, Any]] = []
    try:
        clients, _fresh_center = AimHarderClient(center).list_clients()
    except Exception as exc:
        payload = _payload([], [str(exc)], False)
        _write(payload)
        return payload

    for client in clients:
        if client.get("deactivation_date"):
            continue
        name = _full_name(client)
        signal = signals.find(
            client_id=str(client.get("id") or ""),
            email=str(client.get("email") or ""),
            phone=str(client.get("mobile_number") or client.get("phone") or ""),
            name=name,
        )
        membership = signal.membership_name or ""
        if not membership or _is_excluded_membership(membership):
            continue
        contracted = _contracted_classes(membership)
        if not contracted:
            continue
        paid_at = signal.last_membership_payment_date
        rows.append(
            {
                "center": CENTER_NAME,
                "id": str(client.get("id") or ""),
                "name": name,
                "phone": str(client.get("mobile_number") or client.get("phone") or ""),
                "email": str(client.get("email") or ""),
                "membership_name": membership,
                "cycle_start": paid_at.isoformat() if paid_at else "",
                "contracted_classes": contracted,
                "consumed_classes": None,
                "remaining_classes": None,
                "completion_percent": None,
                "status": "Pendiente histórico AimHarder",
                "last_class_at": _nested(client, "class_data", "reservation_date") or "",
                "source": "AimHarder /clients + CSV pagos Parla",
            }
        )

    rows.sort(key=lambda row: (_date_sort(row.get("cycle_start")), _norm(row.get("name"))), reverse=True)
    payload = _payload(rows, errors, False)
    _write(payload)
    return payload


def _payload(rows: list[dict[str, Any]], errors: list[str], history_available: bool) -> dict[str, Any]:
    return {
        "ok": not errors or bool(rows),
        "center": CENTER_NAME,
        "generated_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "rows": rows,
        "errors": errors,
        "history_available": history_available,
        "kpis": {
            "listed": len(rows),
            "completed": len([row for row in rows if row.get("status") == "Completada"]),
            "pending_history": len([row for row in rows if row.get("consumed_classes") is None]),
        },
    }


def _write(payload: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _contracted_classes(membership: str) -> int | None:
    value = _norm(membership)
    # CFMP: Tarifa S = 9 clases, Tarifa M = 13 clases. Bonos explícitos por número.
    if re.search(r"\b(9)\b", value) or value in {"s", "tarifa s"} or "tarifa s" in value:
        return 9
    if re.search(r"\b(13)\b", value) or value in {"m", "tarifa m"} or "tarifa m" in value:
        return 13
    bono = re.search(r"bono\s+(\d+)\s+clases", value)
    if bono:
        return int(bono.group(1))
    if "academy 2d" in value or "academy 2 dias" in value or "academy 2 dias" in value:
        return 9
    return None


def _is_excluded_membership(membership: str) -> bool:
    value = _norm(membership)
    return any(token in value for token in ("wellhub", "gympass", "congelacion", "congelación"))


def _full_name(client: dict[str, Any]) -> str:
    return " ".join(
        part.strip()
        for part in (
            str(client.get("name") or ""),
            str(client.get("first_surname") or ""),
            str(client.get("second_surname") or ""),
        )
        if part and part.strip()
    )


def _nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _norm(value: Any) -> str:
    stripped = " ".join(str(value or "").split()).lower()
    without_accents = "".join(
        c for c in unicodedata.normalize("NFKD", stripped) if not unicodedata.combining(c)
    )
    return without_accents


def _date_sort(value: Any) -> str:
    return str(value or "")
