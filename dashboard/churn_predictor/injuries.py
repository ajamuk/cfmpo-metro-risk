from __future__ import annotations

import csv
import io
import re
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List


def load_injuries(sheet_url: str, sheet_name: str, center: str | None = None) -> List[dict]:
    if not sheet_url:
        return []
    spreadsheet_id = _spreadsheet_id(sheet_url)
    if not spreadsheet_id:
        return []
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq"
        f"?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "ProyectoRisk/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        text = response.read().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    today = date.today()
    injuries = []
    for row in rows:
        name = _first(row, "NOMBRE", "Nombre del Atleta")
        if not name:
            continue
        last_contact = _parse_date(_first(row, "Fecha Último Contacto"))
        next_contact = _valid_followup_date(_parse_date(_first(row, "Próximo Contacto")))
        injury_type = _int(_first(row, "Tipo de Lesión"))
        if not next_contact and last_contact and injury_type in {1, 2, 3}:
            next_contact = last_contact + timedelta(days={1: 6, 2: 14, 3: 21}[injury_type])
        days_remaining = (next_contact - today).days if next_contact else None
        injuries.append(
            {
                "name": name.strip(),
                "phone": _first(row, "Teléfono"),
                "phone_key": norm_phone(_first(row, "Teléfono")),
                "name_key": norm_name(name),
                "type": injury_type,
                "label": _first(row, "Etiqueta"),
                "follow_up": _first(row, "¿Seguimiento?"),
                "description": _first(row, "Descripción (Qué tiene)"),
                "last_contact": last_contact.isoformat() if last_contact else "",
                "next_contact": next_contact.isoformat() if next_contact else "",
                "days_remaining": days_remaining,
                "status": "Sin seguimiento" if _is_no_followup(_first(row, "¿Seguimiento?")) else ("Pendiente respuesta" if _is_pending_response(_first(row, "¿Seguimiento?")) else _status(days_remaining)),
                "latest_note": _latest_note(row),
                "center": _first(row, "Centro") or center or sheet_name.replace("Beta ", ""),
                "source": _first(row, "Origen"),
                "created_at": _first(row, "Created At"),
                "telegram_chat_id": _first(row, "Telegram Chat ID"),
                "telegram_message_id": _first(row, "Telegram Message ID"),
                "registro_id": _first(row, "Registro ID"),
            }
        )
    return injuries


def load_beta_injuries(sheet_url: str) -> List[dict]:
    items: List[dict] = []
    for center in ("Getafe", "Parla", "Las Rosas"):
        try:
            items.extend(load_injuries(sheet_url, f"Beta {center}", center=center))
        except Exception:
            # Una pestaña beta no debe tumbar todo el dashboard.
            continue
    items.sort(key=lambda item: (item.get("center") or "", 999 if item.get("days_remaining") is None else item["days_remaining"], item.get("name") or ""))
    return items



def load_db_injuries(db_path: str = "/opt/telegram-lesionados/state/lesionados.sqlite") -> List[dict]:
    return _load_db_injuries_by_active(1, db_path)


def load_deleted_db_injuries(db_path: str = "/opt/telegram-lesionados/state/lesionados.sqlite") -> List[dict]:
    return _load_db_injuries_by_active(0, db_path)


def _load_db_injuries_by_active(active: int, db_path: str = "/opt/telegram-lesionados/state/lesionados.sqlite") -> List[dict]:
    import sqlite3
    path = str(db_path or "")
    if not path:
        return []
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM injuries WHERE active=? ORDER BY updated_at DESC, center, COALESCE(next_contact, ''), name", (active,)).fetchall()
    except Exception:
        return []
    today = date.today()
    items = []
    for row in rows:
        name = str(row["name"] or "").strip()
        if not name:
            continue
        next_contact = _valid_followup_date(_parse_date(str(row["next_contact"] or "")))
        last_contact = _parse_date(str(row["last_contact"] or ""))
        injury_type = _int(str(row["injury_type"] or ""))
        if not next_contact and last_contact and injury_type in {1, 2, 3}:
            next_contact = last_contact + timedelta(days={1: 6, 2: 14, 3: 21}[injury_type])
        days_remaining = (next_contact - today).days if next_contact else None
        latest_note = str(row["contact_4"] or row["contact_3"] or row["contact_2"] or row["contact_1"] or "").strip()
        items.append({
            "name": name,
            "phone": str(row["phone"] or ""),
            "phone_key": norm_phone(str(row["phone"] or "")),
            "name_key": norm_name(name),
            "type": injury_type,
            "label": str(row["label"] or ""),
            "follow_up": str(row["follow_up"] or ""),
            "description": str(row["description"] or ""),
            "last_contact": last_contact.isoformat() if last_contact else str(row["last_contact"] or ""),
            "next_contact": next_contact.isoformat() if next_contact else "",
            "days_remaining": days_remaining,
            "status": "Sin seguimiento" if _is_no_followup(str(row["follow_up"] or "")) else ("Pendiente respuesta" if _is_pending_response(str(row["follow_up"] or "")) else _status(days_remaining)),
            "latest_note": latest_note,
            "center": str(row["center"] or ""),
            "source": str(row["source"] or "Base de datos"),
            "created_at": str(row["created_at"] or ""),
            "telegram_chat_id": str(row["telegram_chat_id"] or ""),
            "telegram_message_id": str(row["telegram_message_id"] or ""),
            "registro_id": str(row["registry_id"] or ""),
            "synced_to_sheet_at": str(row["synced_to_sheet_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "active": int(row["active"] or 0),
        })
    try:
        con.close()
    except Exception:
        pass
    return items


def attach_injuries(active_rows: List[dict], injuries: List[dict]) -> tuple[List[dict], List[dict]]:
    by_phone: Dict[str, dict] = {item["phone_key"]: item for item in injuries if item.get("phone_key")}
    by_name: Dict[str, dict] = {item["name_key"]: item for item in injuries if item.get("name_key")}
    linked = []
    for row in active_rows:
        injury = by_phone.get(norm_phone(row.get("telefono", ""))) or by_name.get(norm_name(row.get("nombre", "")))
        row["lesion"] = "si" if injury else "no"
        if injury:
            row["lesion_tipo"] = injury.get("type") or ""
            row["lesion_etiqueta"] = injury.get("label") or ""
            row["lesion_estado"] = injury.get("status") or ""
            row["lesion_proximo_contacto"] = injury.get("next_contact") or ""
            row["lesion_dias_restantes"] = "" if injury.get("days_remaining") is None else injury.get("days_remaining")
            row["lesion_nota"] = injury.get("latest_note") or ""
            linked.append({**injury, "risk": row.get("riesgo", ""), "score": row.get("score", ""), "tariff": row.get("tarifa", ""), "days_without_class": row.get("dias_sin_clase", "")})
        else:
            row["lesion_tipo"] = ""
            row["lesion_etiqueta"] = ""
            row["lesion_estado"] = ""
            row["lesion_proximo_contacto"] = ""
            row["lesion_dias_restantes"] = ""
            row["lesion_nota"] = ""
    linked.sort(key=lambda item: (999 if item.get("days_remaining") is None else item["days_remaining"]))
    return active_rows, linked


def norm_phone(value: str) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def norm_name(value: str) -> str:
    stripped = " ".join(str(value or "").split()).lower()
    return "".join(c for c in unicodedata.normalize("NFKD", stripped) if not unicodedata.combining(c))


def _spreadsheet_id(url: str) -> str:
    match = re.search(r"/spreadsheets/d/([^/]+)", url)
    return match.group(1) if match else url.strip()


def _first(row: dict, *names: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        value = row.get(name)
        if value is None:
            value = lowered.get(name.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _int(value: str) -> int | None:
    try:
        return int(float(str(value).replace(",", ".")))
    except Exception:
        return None


def _valid_followup_date(value: date | None) -> date | None:
    # Fechas demasiado antiguas suelen venir de campos cruzados (p.ej. nacimiento), no de seguimiento.
    if value and value < date(2024, 1, 1):
        return None
    return value


def _is_no_followup(value: str) -> bool:
    normalized = norm_name(value)
    return normalized in {"no", "n", "false", "0", "sin seguimiento"}


def _is_pending_response(value: str) -> bool:
    normalized = norm_name(value)
    return normalized in {"pendiente respuesta", "pendiente de respuesta", "esperando respuesta"}


def _status(days_remaining: int | None) -> str:
    if days_remaining is None:
        return "Sin fecha"
    if days_remaining < 0:
        return "Vencido"
    if days_remaining == 0:
        return "Hoy"
    if days_remaining <= 7:
        return "Proximos 7 dias"
    return "Al dia"


def _latest_note(row: dict) -> str:
    notes = []
    for idx in range(1, 8):
        value = _first(row, f"Contacto {idx}")
        if value:
            notes.append(value)
    return notes[-1] if notes else ""

