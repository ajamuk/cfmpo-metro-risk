from __future__ import annotations

import csv
import base64
import hmac
import json
import subprocess
import sys
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.append('/opt/cfmpo-client-profiles')
from client_profiles import add_note, get_profile, upsert_client

from .config import ROOT, load_settings
from .injuries import attach_injuries, attach_tariffs_to_injuries, load_beta_injuries, load_db_injuries, load_deleted_db_injuries, load_injuries
from .local_data import load_signals
from .inactivity import load_inactive_members_cache, refresh_inactive_members
from .inactive_workflow import enrich_inactive_rows, mark_inactive_workflow
from .tariff_completions import load_tariff_completions_cache, refresh_tariff_completions
from . import ghl as ghl_mod

_LAST_PROFILE_SEED_SIGNATURE = None


@dataclass
class LatestReport:
    path: Path | None
    rows: list[dict]
    generated_at: str


def latest_report() -> LatestReport:
    settings = load_settings()
    files = sorted(settings.report_dir.glob("posibles_bajas_*.csv"), reverse=True)
    if not files:
        return LatestReport(path=None, rows=[], generated_at="")
    path = files[0]
    with path.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    generated_at = datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
    return LatestReport(path=path, rows=rows, generated_at=generated_at)


def dashboard_payload() -> dict:
    settings = load_settings()
    report = latest_report()
    rows = report.rows
    legacy_injuries = load_injuries(settings.injuries_sheet_url, settings.injuries_sheet_name)
    db_injuries = load_db_injuries()
    deleted_injuries = load_deleted_db_injuries()
    beta_injuries = load_beta_injuries(settings.injuries_sheet_url)
    injuries = db_injuries or beta_injuries or legacy_injuries
    injuries = attach_tariffs_to_injuries(injuries, load_signals(settings.data_dir))
    rows, linked_injuries = attach_injuries(rows, injuries)
    seed_client_profiles(rows, injuries, report.generated_at)
    high = [row for row in rows if row.get("riesgo") == "Alto"]
    medium = [row for row in rows if row.get("riesgo") == "Medio"]
    low = [row for row in rows if row.get("riesgo") == "Bajo"]
    stale_payment = [row for row in rows if "75 dias" in row.get("motivos", "")]
    no_class_30 = [
        row for row in rows
        if row.get("dias_sin_clase", "").isdigit() and int(row["dias_sin_clase"]) > 30
    ]
    injury_due = [
        item for item in linked_injuries
        if item.get("status") in {"Vencido", "Hoy", "Proximos 7 dias"}
    ]
    inactive_members = load_inactive_members_cache()
    inactive_rows = enrich_inactive_rows(inactive_members.get("rows", []))
    tariff_completions = load_tariff_completions_cache()
    return {
        "generated_at": report.generated_at,
        "report_file": report.path.name if report.path else "",
        "rows": rows,
        "injuries": linked_injuries,
        "injuries_app": injuries,
        "deleted_injuries": deleted_injuries,
        "inactive_members": inactive_rows,
        "inactive_members_generated_at": inactive_members.get("generated_at", ""),
        "inactive_members_errors": inactive_members.get("errors", []),
        "inactive_members_threshold_days": inactive_members.get("threshold_days", 7),
        "inactive_members_kpis": inactive_members.get("kpis", {}),
        "inactive_members_centers": inactive_members.get("centers", {}),
        "tariff_completions": tariff_completions.get("rows", []),
        "tariff_completions_generated_at": tariff_completions.get("generated_at", ""),
        "tariff_completions_errors": tariff_completions.get("errors", []),
        "tariff_completions_kpis": tariff_completions.get("kpis", {}),
        "tariff_completions_history_available": tariff_completions.get("history_available", False),
        "injury_centers": {
            "Getafe": len([item for item in injuries if item.get("center") == "Getafe"]),
            "Parla": len([item for item in injuries if item.get("center") == "Parla"]),
            "Las Rosas": len([item for item in injuries if item.get("center") == "Las Rosas"]),
        },
        "kpis": {
            "listed": len(rows),
            "high": len(high),
            "medium": len(medium),
            "low": len(low),
            "stale_payment": len(stale_payment),
            "no_class_30": len(no_class_30),
            "injured": len(linked_injuries),
            "injury_due": len(injury_due),
            "inactive_7d": len(inactive_rows),
            "avg_score": round(sum(int(row.get("score") or 0) for row in rows) / len(rows), 1) if rows else 0,
        },
    }


def seed_client_profiles(rows: list[dict], injuries: list[dict], generated_at: str = "") -> None:
    global _LAST_PROFILE_SEED_SIGNATURE
    signature = (generated_at, len(rows), len(injuries))
    if _LAST_PROFILE_SEED_SIGNATURE == signature:
        return
    try:
        for row in rows:
            upsert_client({
                "name": row.get("nombre") or "",
                "phone": row.get("telefono") or "",
                "email": row.get("email") or "",
                "center": row.get("centro") or row.get("center") or row.get("sede") or "",
                "external_id": row.get("id") or "",
                "source": "risk-auto",
            })
        for item in injuries:
            upsert_client({
                "name": item.get("name") or "",
                "phone": item.get("phone") or "",
                "email": item.get("email") or "",
                "center": item.get("center") or "",
                "external_id": item.get("external_id") or "",
                "source": "lesionados-auto",
            })
        _LAST_PROFILE_SEED_SIGNATURE = signature
    except Exception as exc:
        print(f"[risk] seed_client_profiles failed: {exc}", file=sys.stderr)


def _client_payload_from_row(row: dict) -> dict:
    return {
        "name": row.get("nombre") or row.get("name") or "",
        "phone": row.get("telefono") or row.get("phone") or "",
        "email": row.get("email") or "",
        "center": row.get("centro") or row.get("center") or row.get("sede") or row.get("box") or "",
        "external_id": row.get("id") or row.get("external_id") or "",
        "source": "risk",
    }


def sync_injury_registry_to_beta(registry_id: str) -> dict:
    try:
        sys.path.insert(0, '/opt/telegram-lesionados')
        from sheets_sync import sync_record_to_sheet
        ok = sync_record_to_sheet(registry_id)
        return {"ok": bool(ok)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def update_injury_details(registry_id: str, injury_type: str, description: str, db_path: str = "/opt/telegram-lesionados/state/lesionados.sqlite") -> dict:
    import sqlite3

    registry_id = str(registry_id or "").strip()
    injury_type = str(injury_type or "").strip()
    description = str(description or "").strip()
    if not registry_id:
        raise ValueError("Falta registro_id")
    if not injury_type:
        raise ValueError("Falta tipo de lesión")
    if injury_type not in {"1", "2", "3"}:
        raise ValueError("Tipo de lesión inválido")
    if len(description) > 1200:
        raise ValueError("Motivo demasiado largo")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM injuries WHERE registry_id=? AND active=1", (registry_id,)).fetchone()
        if not row:
            raise ValueError("Lesionado no encontrado")
        today = date.today()
        now = datetime.now().isoformat(timespec="seconds")
        note = f"{today.isoformat()} editado desde ficha: tipo='{injury_type}', motivo='{description[:220]}'"
        target, value = _append_injury_note(row, note)
        label = description[:120] if description else str(row["label"] or "").strip()
        con.execute(
            f"UPDATE injuries SET injury_type=?, label=?, description=?, {target}=?, synced_to_sheet_at=NULL, updated_at=? WHERE registry_id=?",
            (injury_type, label, description, value, now, registry_id),
        )
        con.commit()
        sync = sync_injury_registry_to_beta(registry_id)
        return {"ok": True, "registry_id": registry_id, "injury_type": injury_type, "label": label, "description": description, "note_field": target, "sync": sync}
    finally:
        con.close()


def remove_injury_followup(registry_id: str, db_path: str = "/opt/telegram-lesionados/state/lesionados.sqlite") -> dict:
    import sqlite3

    registry_id = str(registry_id or "").strip()
    if not registry_id:
        raise ValueError("Falta registro_id")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM injuries WHERE registry_id=? AND active=1", (registry_id,)).fetchone()
        if not row:
            raise ValueError("Lesionado no encontrado o ya eliminado")
        today = date.today()
        now = datetime.now().isoformat(timespec="seconds")
        history = _injury_history_summary(row)
        note = f"{today.isoformat()} eliminado del seguimiento desde panel. Historial: {history}"
        target, value = _append_injury_note(row, note)
        con.execute(
            f"UPDATE injuries SET active=0, {target}=?, synced_to_sheet_at=NULL, updated_at=? WHERE registry_id=?",
            (value, now, registry_id),
        )
        con.commit()
        sync = sync_injury_registry_to_beta(registry_id)
        return {"ok": True, "registry_id": registry_id, "removed_at": now, "note_field": target, "sync": sync}
    finally:
        con.close()


def _append_injury_note(row, note: str) -> tuple[str, str]:
    contacts = {f"contact_{i}": str(row[f"contact_{i}"] or "") for i in range(1, 5)}
    target = next((key for key, value in contacts.items() if not value.strip()), "contact_4")
    value = f"{contacts[target].strip()} | {note}" if contacts[target].strip() else note
    return target, value


def _injury_history_summary(row) -> str:
    parts = []
    for label, key in [
        ("Centro", "center"), ("Nombre", "name"), ("Tel", "phone"), ("Tipo", "injury_type"),
        ("Etiqueta", "label"), ("Lesión", "description"), ("Último", "last_contact"), ("Próximo", "next_contact"),
    ]:
        value = str(row[key] or "").strip()
        if value:
            parts.append(f"{label}: {value}")
    notes = [str(row[f"contact_{i}"] or "").strip() for i in range(1, 5) if str(row[f"contact_{i}"] or "").strip()]
    if notes:
        parts.append("Notas previas: " + " / ".join(notes))
    return "; ".join(parts)[:1800]


def mark_injury_followup_done(registry_id: str, db_path: str = "/opt/telegram-lesionados/state/lesionados.sqlite") -> dict:
    import sqlite3

    registry_id = str(registry_id or "").strip()
    if not registry_id:
        raise ValueError("Falta registro_id")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM injuries WHERE registry_id=? AND active=1", (registry_id,)).fetchone()
        if not row:
            raise ValueError("Lesionado no encontrado")
        today = date.today()
        injury_type = _int_like(row["injury_type"])
        interval_days = {1: 6, 2: 14, 3: 21}.get(injury_type, 7)
        next_contact = today + timedelta(days=interval_days)
        now = datetime.now().isoformat(timespec="seconds")
        note = f"{today.isoformat()} seguimiento marcado como hecho desde panel. Próximo: {next_contact.isoformat()}"
        target, value = _append_injury_note(row, note)
        sql = (
            "UPDATE injuries "
            f"SET follow_up=?, last_contact=?, next_contact=?, days_remaining=?, {target}=?, synced_to_sheet_at=NULL, updated_at=? "
            "WHERE registry_id=?"
        )
        con.execute(sql, ("Si", today.isoformat(), next_contact.isoformat(), str(interval_days), value, now, registry_id))
        con.commit()
        sync = sync_injury_registry_to_beta(registry_id)
        return {
            "ok": True,
            "registry_id": registry_id,
            "sync": sync,
            "last_contact": today.isoformat(),
            "next_contact": next_contact.isoformat(),
            "days_remaining": interval_days,
            "interval_days": interval_days,
            "note_field": target,
        }
    finally:
        con.close()


def mark_injury_pending_response(registry_id: str, db_path: str = "/opt/telegram-lesionados/state/lesionados.sqlite") -> dict:
    import sqlite3

    registry_id = str(registry_id or "").strip()
    if not registry_id:
        raise ValueError("Falta registro_id")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM injuries WHERE registry_id=? AND active=1", (registry_id,)).fetchone()
        if not row:
            raise ValueError("Lesionado no encontrado")
        today = date.today()
        now = datetime.now().isoformat(timespec="seconds")
        note = f"{today.isoformat()} marcado como pendiente de respuesta desde panel."
        target, value = _append_injury_note(row, note)
        sql = (
            "UPDATE injuries "
            f"SET follow_up=?, {target}=?, synced_to_sheet_at=NULL, updated_at=? "
            "WHERE registry_id=?"
        )
        con.execute(sql, ("Pendiente respuesta", value, now, registry_id))
        con.commit()
        sync = sync_injury_registry_to_beta(registry_id)
        return {"ok": True, "registry_id": registry_id, "status": "Pendiente respuesta", "note_field": target, "sync": sync}
    finally:
        con.close()


def add_injury_followup_note(registry_id: str, note_text: str, db_path: str = "/opt/telegram-lesionados/state/lesionados.sqlite") -> dict:
    import sqlite3

    registry_id = str(registry_id or "").strip()
    note_text = str(note_text or "").strip()
    if not registry_id:
        raise ValueError("Falta registro_id")
    if not note_text:
        raise ValueError("La nota está vacía")
    if len(note_text) > 1200:
        raise ValueError("La nota es demasiado larga")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM injuries WHERE registry_id=? AND active=1", (registry_id,)).fetchone()
        if not row:
            raise ValueError("Lesionado no encontrado")
        today = date.today()
        now = datetime.now().isoformat(timespec="seconds")
        note = f"{today.isoformat()} nota desde panel: {note_text}"
        target, value = _append_injury_note(row, note)
        con.execute(
            f"UPDATE injuries SET {target}=?, synced_to_sheet_at=NULL, updated_at=? WHERE registry_id=?",
            (value, now, registry_id),
        )
        con.commit()
        sync = sync_injury_registry_to_beta(registry_id)
        return {"ok": True, "registry_id": registry_id, "note_field": target, "sync": sync}
    finally:
        con.close()


def reschedule_injury_followup(registry_id: str, days: int, db_path: str = "/opt/telegram-lesionados/state/lesionados.sqlite") -> dict:
    import sqlite3

    registry_id = str(registry_id or "").strip()
    if not registry_id:
        raise ValueError("Falta registro_id")
    try:
        days = int(days)
    except Exception:
        raise ValueError("Días inválidos")
    if days not in (1, 2):
        raise ValueError("Solo se permite reprogramar a 1 o 2 días")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM injuries WHERE registry_id=? AND active=1", (registry_id,)).fetchone()
        if not row:
            raise ValueError("Lesionado no encontrado")
        today = date.today()
        next_contact = today + timedelta(days=days)
        now = datetime.now().isoformat(timespec="seconds")
        label = "mañana" if days == 1 else "48h"
        note = f"{today.isoformat()} seguimiento reprogramado para {label} desde panel. Próximo: {next_contact.isoformat()}"
        target, value = _append_injury_note(row, note)
        sql = (
            "UPDATE injuries "
            f"SET follow_up=?, next_contact=?, days_remaining=?, {target}=?, synced_to_sheet_at=NULL, updated_at=? "
            "WHERE registry_id=?"
        )
        con.execute(sql, ("Si", next_contact.isoformat(), str(days), value, now, registry_id))
        con.commit()
        sync = sync_injury_registry_to_beta(registry_id)
        return {"ok": True, "registry_id": registry_id, "next_contact": next_contact.isoformat(), "days_remaining": days, "note_field": target, "sync": sync}
    finally:
        con.close()


def _int_like(value) -> int | None:
    try:
        return int(float(str(value or "").replace(",", ".")))
    except Exception:
        return None


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ChurnDashboard/1.0"

    def do_HEAD(self) -> None:
        if not self._authorized():
            self._request_auth()
            return
        if urlparse(self.path).path == "/":
            data = INDEX_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:
        if not self._authorized():
            self._request_auth()
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(INDEX_HTML)
            return
        if parsed.path == "/api/latest":
            self._send_json(dashboard_payload())
            return
        if parsed.path == "/api/client-profile":
            from urllib.parse import parse_qs
            key = parse_qs(parsed.query).get("client_key", [""])[0]
            self._send_json(get_profile(key) if key else {})
            return
        if parsed.path == "/api/inactive-members":
            data = load_inactive_members_cache()
            data["rows"] = enrich_inactive_rows(data.get("rows", []))
            self._send_json(data)
            return
        if parsed.path == "/api/tariff-completions":
            self._send_json(load_tariff_completions_cache())
            return
        if parsed.path == "/api/ghl/open":
            from urllib.parse import parse_qs
            phone = parse_qs(parsed.query).get("phone", [""])[0]
            try:
                result = ghl_mod.resolve_conversation(phone)
            except Exception as exc:
                result = {"ok": False, "error": f"Error inesperado: {exc}"}
            if result.get("ok") and result.get("conversation_url"):
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", result["conversation_url"])
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            self._send_json(result, status=HTTPStatus.NOT_FOUND if "no encontrado" in str(result.get("error", "")).lower() else HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path.startswith("/reports/"):
            self._send_report(parsed.path.removeprefix("/reports/"))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._authorized():
            self._request_auth()
            return
        parsed_path = urlparse(self.path).path
        if parsed_path == "/api/inactive-refresh":
            try:
                self._send_json(refresh_inactive_members())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed_path == "/api/inactive-workflow":
            try:
                raw = self.rfile.read(min(int(self.headers.get('content-length', '0')), 50000))
                body = json.loads(raw.decode() or '{}')
                self._send_json(mark_inactive_workflow(body))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed_path == "/api/tariff-completions-refresh":
            try:
                self._send_json(refresh_tariff_completions())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed_path == "/api/client-profile":
            try:
                raw = self.rfile.read(min(int(self.headers.get('content-length', '0')), 50000))
                body = json.loads(raw.decode() or '{}')
                profile = upsert_client(body)
                self._send_json(profile)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed_path == "/api/client-note":
            try:
                raw = self.rfile.read(min(int(self.headers.get('content-length', '0')), 50000))
                body = json.loads(raw.decode() or '{}')
                profile = upsert_client(body.get('client') or {})
                key = profile.get('client', {}).get('client_key') or body.get('client_key')
                note = str(body.get('note') or '').strip()
                if not key or not note:
                    raise ValueError('Falta cliente o nota')
                self._send_json(add_note(key, note, body.get('author') or 'Paquita', body.get('source_app') or 'risk'))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed_path == "/api/injury-details-update":
            try:
                raw = self.rfile.read(min(int(self.headers.get('content-length', '0')), 30000))
                body = json.loads(raw.decode() or '{}')
                self._send_json(update_injury_details(body.get('registro_id') or body.get('registry_id'), body.get('injury_type') or body.get('tipo') or '', body.get('description') or body.get('motivo') or ''))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed_path == "/api/injury-followup-done":
            try:
                raw = self.rfile.read(min(int(self.headers.get('content-length', '0')), 20000))
                body = json.loads(raw.decode() or '{}')
                self._send_json(mark_injury_followup_done(body.get('registro_id') or body.get('registry_id')))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed_path == "/api/injury-followup-note":
            try:
                raw = self.rfile.read(min(int(self.headers.get('content-length', '0')), 30000))
                body = json.loads(raw.decode() or '{}')
                self._send_json(add_injury_followup_note(body.get('registro_id') or body.get('registry_id'), body.get('note') or body.get('nota') or ''))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed_path == "/api/injury-followup-pending-response":
            try:
                raw = self.rfile.read(min(int(self.headers.get('content-length', '0')), 20000))
                body = json.loads(raw.decode() or '{}')
                self._send_json(mark_injury_pending_response(body.get('registro_id') or body.get('registry_id')))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed_path == "/api/injury-followup-reschedule":
            try:
                raw = self.rfile.read(min(int(self.headers.get('content-length', '0')), 20000))
                body = json.loads(raw.decode() or '{}')
                self._send_json(reschedule_injury_followup(body.get('registro_id') or body.get('registry_id'), body.get('days') or body.get('dias') or 1))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed_path == "/api/injury-followup-remove":
            try:
                raw = self.rfile.read(min(int(self.headers.get('content-length', '0')), 20000))
                body = json.loads(raw.decode() or '{}')
                self._send_json(remove_injury_followup(body.get('registro_id') or body.get('registry_id')))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed_path != "/api/refresh":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        proc = subprocess.run(
            [sys.executable, str(ROOT / "run_daily.py")],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=180,
        )
        payload = dashboard_payload()
        payload["refresh"] = {
            "ok": proc.returncode == 0,
            "output": proc.stdout.strip(),
            "error": proc.stderr.strip(),
        }
        status = HTTPStatus.OK if proc.returncode == 0 else HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_json(payload, status=status)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _authorized(self) -> bool:
        settings = load_settings()
        if not settings.dashboard_user or not settings.dashboard_password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode("utf-8")
        except Exception:
            return False
        user, sep, password = decoded.partition(":")
        if not sep:
            return False
        return (
            hmac.compare_digest(user, settings.dashboard_user)
            and hmac.compare_digest(password, settings.dashboard_password)
        )

    def _request_auth(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Proyecto Risk"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Autenticacion requerida".encode("utf-8"))

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_report(self, name: str) -> None:
        safe_name = Path(unquote(name)).name
        path = load_settings().report_dir / safe_name
        if not path.exists() or path.suffix not in {".csv", ".html"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        content_type = "text/csv; charset=utf-8" if path.suffix == ".csv" else "text/html; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


INDEX_HTML = r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Proyecto Risk - CrossFit MPO</title>
  <style>
    :root {
      --bg: #f5f6f2;
      --ink: #161817;
      --muted: #686d67;
      --line: #dfe3dc;
      --panel: #ffffff;
      --green: #87b15f;
      --green-dark: #52733a;
      --red: #c94b45;
      --amber: #c48c2b;
      --shadow: 0 18px 45px rgba(18, 18, 18, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      letter-spacing: 0;
    }
    .shell { min-height: 100vh; display: grid; grid-template-columns: 230px 1fr; }
    aside {
      background: #151715;
      color: white;
      padding: 24px 18px;
      display: flex;
      flex-direction: column;
      gap: 28px;
    }
    .brand { display: grid; gap: 6px; }
    .brand strong { font-size: 17px; line-height: 1.15; }
    .brand span { color: #bfc8b8; font-size: 13px; }
    .nav { display: grid; gap: 8px; }
    .nav button {
      border: 0;
      width: 100%;
      display: flex;
      gap: 10px;
      align-items: center;
      color: #e9eee5;
      background: transparent;
      padding: 10px 12px;
      border-radius: 8px;
      font: inherit;
      font-size: 14px;
      cursor: pointer;
    }
    .nav button.active, .nav button:hover { background: rgba(135,177,95,.18); color: white; }
    .main { padding: 24px; min-width: 0; }
    .topbar {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 22px;
    }
    h1 { margin: 0 0 6px; font-size: clamp(26px, 3vw, 38px); line-height: 1.05; }
    .subtitle { margin: 0; color: var(--muted); font-size: 14px; }
    .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .button {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      border-radius: 8px;
      min-height: 38px;
      padding: 0 13px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      text-decoration: none;
      font-weight: 650;
      font-size: 13px;
      cursor: pointer;
      box-shadow: 0 1px 0 rgba(0,0,0,.03);
    }
    .button.primary { background: var(--green); border-color: var(--green); color: #101510; }
    .button:disabled { opacity: .65; cursor: wait; }
    .kpis { display: grid; grid-template-columns: repeat(8, minmax(110px, 1fr)); gap: 12px; margin-bottom: 18px; }
    .kpi {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 15px;
      box-shadow: var(--shadow);
      min-height: 104px;
    }
    .kpi span { color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
    .kpi strong { display: block; margin-top: 10px; font-size: 30px; line-height: 1; }
    .kpi small { display: block; margin-top: 8px; color: var(--muted); font-size: 12px; }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto auto;
      gap: 10px;
      align-items: center;
      margin-bottom: 12px;
    }
    .tabs {
      display: inline-flex;
      background: #e9ece5;
      padding: 3px;
      border-radius: 8px;
      gap: 3px;
      margin-bottom: 16px;
    }
    .tabs button {
      border: 0;
      background: transparent;
      border-radius: 7px;
      min-height: 34px;
      padding: 0 14px;
      font: inherit;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      color: #454a45;
    }
    .tabs button.active { background: var(--panel); color: var(--ink); box-shadow: 0 1px 3px rgba(0,0,0,.08); }
    .tab-panel[hidden] { display: none; }
    .rules {
      display: grid;
      grid-template-columns: .8fr 1.6fr;
      gap: 12px;
      margin-bottom: 18px;
    }
    .rules-panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 15px;
      box-shadow: var(--shadow);
    }
    .rules-panel h2 {
      margin: 0 0 10px;
      font-size: 14px;
      line-height: 1.2;
    }
    .legend {
      display: grid;
      gap: 8px;
    }
    .legend-row {
      display: grid;
      grid-template-columns: 82px 1fr;
      gap: 10px;
      align-items: center;
      font-size: 13px;
      color: var(--muted);
    }
    .rule-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .rule-block {
      border: 1px solid #edf0ea;
      border-radius: 8px;
      padding: 10px;
      min-height: 96px;
    }
    .rule-block strong {
      display: block;
      font-size: 13px;
      text-transform: uppercase;
      color: #4d534e;
      margin-bottom: 8px;
    }
    .rule-block p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .settings {
      display: grid;
      gap: 14px;
    }
    .settings-panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 15px;
      box-shadow: var(--shadow);
    }
    .settings-panel h2 {
      margin: 0 0 10px;
      font-size: 14px;
      line-height: 1.2;
    }
    .profile-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    .profile-card {
      border: 1px solid var(--line);
      background: #fbfcfa;
      border-radius: 8px;
      padding: 12px;
      cursor: pointer;
      text-align: left;
      font: inherit;
    }
    .profile-card.active {
      border-color: var(--green);
      background: #f0f7e9;
    }
    .profile-card strong { display: block; font-size: 14px; margin-bottom: 6px; }
    .profile-card span { color: var(--muted); font-size: 12px; line-height: 1.35; display: block; }
    .control-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .slider-row {
      border: 1px solid #edf0ea;
      border-radius: 8px;
      padding: 10px;
    }
    .slider-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 8px;
    }
    .slider-head label { font-size: 13px; font-weight: 800; color: #3f453f; }
    .slider-value { font-size: 13px; font-weight: 850; color: var(--green-dark); }
    input[type="range"] { width: 100%; accent-color: var(--green); }
    .search {
      width: 100%;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      height: 40px;
      padding: 0 12px;
      font: inherit;
      font-size: 14px;
      outline: none;
    }
    .segmented {
      display: inline-flex;
      background: #e9ece5;
      padding: 3px;
      border-radius: 8px;
      gap: 3px;
    }
    .segmented button {
      border: 0;
      background: transparent;
      border-radius: 7px;
      height: 34px;
      padding: 0 12px;
      font: inherit;
      font-size: 13px;
      cursor: pointer;
      color: #454a45;
      white-space: nowrap;
    }
    .segmented button.active { background: var(--panel); color: var(--ink); box-shadow: 0 1px 3px rgba(0,0,0,.08); }
    .table-wrap {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    table { width: 100%; min-width: 1020px; border-collapse: collapse; font-size: 13px; }
    th {
      background: #fbfcfa;
      color: #4d534e;
      text-align: left;
      font-size: 12px;
      text-transform: uppercase;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      white-space: nowrap;
      position: sticky;
      top: 0;
      z-index: 1;
    }
    th button {
      width: 100%;
      border: 0;
      padding: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      text-transform: inherit;
      display: inline-flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      cursor: pointer;
      text-align: left;
    }
    th button::after {
      content: "↕";
      color: #9aa198;
      font-size: 11px;
    }
    th button.active.desc::after { content: "↓"; color: var(--green-dark); }
    th button.active.asc::after { content: "↑"; color: var(--green-dark); }
    td { padding: 13px 14px; border-bottom: 1px solid #edf0ea; vertical-align: top; }
    tr:hover td { background: #fbfcf8; }
    .score { font-weight: 800; font-size: 16px; display: grid; gap: 7px; }
    .risk {
      display: inline-flex;
      align-items: center;
      min-width: 62px;
      justify-content: center;
      height: 25px;
      border-radius: 999px;
      font-weight: 800;
      font-size: 12px;
    }
    .risk.Alto { background: #ffe8e5; color: var(--red); }
    .risk.Medio { background: #fff4d8; color: var(--amber); }
    .risk.Bajo { background: #eaf4e1; color: var(--green-dark); }
    .risk.Vencido { background: #ffe8e5; color: var(--red); }
    .risk.Hoy { background: #fff4d8; color: var(--amber); }
    .risk.Proximos { background: #e7f0ff; color: #3267a8; }
    .risk.Al.dia { background: #eaf4e1; color: var(--green-dark); }
    .name { font-weight: 760; white-space: nowrap; }
    .contact { color: var(--muted); line-height: 1.45; min-width: 170px; overflow-wrap: anywhere; }
    .ghl-btn { display: inline-block; padding: 1px 7px; margin-left: 6px; font-size: 11px; font-weight: 700; line-height: 1.4; color: var(--green-dark); background: rgba(135,177,95,.14); border: 1px solid rgba(135,177,95,.4); border-radius: 999px; text-decoration: none; white-space: nowrap; vertical-align: 1px; }
    .ghl-btn:hover { background: var(--green); color: #101510; border-color: var(--green); }
    .reasons { min-width: 280px; max-width: 430px; line-height: 1.45; }
    .muted { color: var(--muted); }
    .empty { padding: 36px; text-align: center; color: var(--muted); }
    @media (max-width: 980px) {
      .shell { grid-template-columns: 1fr; }
      aside { display: none; }
      .main { padding: 18px; }
      .topbar { flex-direction: column; }
      .actions { justify-content: flex-start; }
      .kpis { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
      .toolbar { grid-template-columns: 1fr; }
      .rules { grid-template-columns: 1fr; }
      .rule-grid { grid-template-columns: 1fr; }
      .profile-grid, .control-grid { grid-template-columns: 1fr; }
      .table-wrap { overflow-x: auto; }
      table { min-width: 1020px; }
    }


    /* CFMP visual hardening — legibilidad y uso real */
    :root {
      --bg: #0f120f;
      --ink: #f3f6ef;
      --muted: #aeb8a8;
      --line: #2f382c;
      --panel: #171d15;
      --panel-soft: #20271d;
      --green: #88AF60;
      --green-dark: #a8d37a;
      --red: #ff7b72;
      --amber: #f0b85a;
      --shadow: 0 22px 70px rgba(0, 0, 0, .28);
    }
    body { background: radial-gradient(circle at top left, #24311f 0, #0f120f 44%, #090b09 100%); color: var(--ink); }
    .shell { grid-template-columns: 245px minmax(0, 1fr); }
    aside { background: linear-gradient(180deg, #11160f, #090b09); border-right: 1px solid var(--line); position: relative; overflow: hidden; }
    aside::after { content: none; }
    .brand span, .subtitle, .muted, .kpi small, .kpi span, .contact, .rule-block p, .legend-row, .profile-card span { color: var(--muted); }
    .main { max-width: 1680px; width: 100%; }
    .topbar { background: rgba(23,29,21,.74); border: 1px solid var(--line); border-radius: 22px; padding: 18px; box-shadow: var(--shadow); }
    h1 { letter-spacing: -.045em; }
    .button, .search, .kpi, .rules-panel, .settings-panel, .profile-card, .slider-row, .table-wrap { background: var(--panel); border-color: var(--line); color: var(--ink); }
    .button:hover, .profile-card:hover { border-color: rgba(136,175,96,.75); }
    .button.primary { background: var(--green); border-color: var(--green); color: #101510; }
    .tabs, .segmented { background: #10150f; border: 1px solid var(--line); }
    .tabs button, .segmented button { color: #dfe8da; }
    .tabs button.active, .segmented button.active { background: var(--green); color: #101510; box-shadow: none; }
    .kpis { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
    .kpi { border-radius: 18px; min-height: 112px; }
    .kpi strong { color: #fff; }
    .toolbar { grid-template-columns: minmax(260px, 1fr) auto auto; }
    .search { height: 44px; }
    .table-wrap { border-radius: 18px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
    table { min-width: 1180px; color: var(--ink); }
    th { background: #20271d; color: #dfe8da; border-bottom-color: var(--line); }
    th button::after { color: #90a18a; }
    td { border-bottom-color: #293126; color: #eef4e8; }
    tbody tr:nth-child(even) td { background: rgba(255,255,255,.018); }
    tr:hover td { background: rgba(136,175,96,.10); }
    .name { color: #ffffff; }
    .contact { color: #c5d0bf; }
    .reasons { color: #d6dfd0; min-width: 330px; }
    .risk.Alto, .risk.Vencido { background: rgba(255,123,114,.16); color: #ffafa9; border: 1px solid rgba(255,123,114,.26); }
    .risk.Medio, .risk.Hoy { background: rgba(240,184,90,.18); color: #ffd28b; border: 1px solid rgba(240,184,90,.26); }
    .risk.Bajo, .risk.Al.dia { background: rgba(136,175,96,.18); color: #bce28d; border: 1px solid rgba(136,175,96,.28); }
    .risk.Proximos { background: rgba(104,154,255,.18); color: #abc8ff; border: 1px solid rgba(104,154,255,.28); }
    .rule-block { border-color: var(--line); background: var(--panel-soft); }
    .rule-block strong, .slider-head label { color: #eef4e8; }
    .profile-card.active { background: rgba(136,175,96,.14); border-color: var(--green); }
    input[type="range"] { accent-color: var(--green); }
    .empty { color: var(--muted); }
    @media (max-width: 1180px) {
      .shell { grid-template-columns: 1fr; }
      aside { display: none; }
      .main { padding: 16px; }
    }
    @media (max-width: 760px) {
      .main { padding: 12px; }
      .topbar { border-radius: 18px; padding: 14px; }
      .actions, .segmented { width: 100%; }
      .button { flex: 1; justify-content: center; }
      .toolbar { grid-template-columns: 1fr; }
      .segmented { display: grid; grid-template-columns: repeat(4, 1fr); }
      .segmented button { padding: 0 8px; }
      .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
      .kpi { padding: 13px; min-height: 96px; }
      .kpi strong { font-size: 26px; }
      table { min-width: 1060px; }
    }



    /* CFMP table fit — evitar columnas cortadas en escritorio */
    .shell { grid-template-columns: 220px minmax(0, 1fr); }
    aside { box-shadow: none; }
    .table-wrap { overflow-x: visible; }
    table { min-width: 0; width: 100%; table-layout: fixed; font-size: 12px; }
    th, td { padding: 11px 10px; overflow-wrap: anywhere; word-break: normal; }
    th:nth-child(1), td:nth-child(1) { width: 8%; }
    th:nth-child(2), td:nth-child(2) { width: 18%; }
    th:nth-child(3), td:nth-child(3) { width: 18%; }
    th:nth-child(4), td:nth-child(4) { width: 9%; }
    th:nth-child(5), td:nth-child(5) { width: 13%; }
    th:nth-child(6), td:nth-child(6) { width: 12%; }
    th:nth-child(7), td:nth-child(7) { width: 22%; }
    .name { white-space: normal; line-height: 1.25; }
    .contact, .reasons { min-width: 0; max-width: none; }
    .score { font-size: 15px; }
    .risk { min-width: 0; width: max-content; max-width: 100%; padding: 0 9px; }
    @media (max-width: 1180px) {
      .table-wrap { overflow-x: auto; }
      table { min-width: 1060px; table-layout: auto; }
    }



    /* CFMP header readability */
    th { white-space: normal; line-height: 1.15; vertical-align: bottom; }
    th button { display: block; line-height: 1.15; white-space: normal; }
    th button::after { content: ""; }
    th:nth-child(4), td:nth-child(4) { width: 11%; }
    th:nth-child(5), td:nth-child(5) { width: 15%; }
    th:nth-child(7), td:nth-child(7) { width: 18%; }



    /* CFMP unified alignment — usa el lenguaje visual común de /cfmpo-unified.css */
    :root {
      --bg: var(--cf-bg, #090B09);
      --ink: var(--cf-text, #EFE9E9);
      --muted: var(--cf-muted, #C3BFBE);
      --line: var(--cf-line, rgba(239,233,233,.13));
      --panel: var(--cf-panel, rgba(18,18,18,.86));
      --panel-soft: var(--cf-panel2, rgba(28,31,27,.78));
      --green: var(--cf-green, #88AF60);
      --green-dark: var(--cf-green2, #A6C977);
      --red: var(--cf-danger, #E36B5D);
      --amber: #E6B35F;
      --shadow: var(--cf-shadow, 0 24px 80px rgba(0,0,0,.34));
    }
    .shell {
      display: block !important;
      width: min(calc(100% - 32px), 1440px) !important;
      max-width: 1440px !important;
      margin: 0 auto !important;
      min-height: auto;
    }
    aside { display: none !important; }
    .main {
      width: 100% !important;
      max-width: 1440px !important;
      margin: 0 auto !important;
      padding: 0 0 28px !important;
    }
    .topbar {
      width: 100% !important;
      margin: 0 0 18px !important;
      border-radius: var(--cf-radius, 22px) !important;
      background: linear-gradient(180deg, rgba(31,36,30,.70), rgba(18,18,18,.70)) !important;
      border: 1px solid var(--cf-line, rgba(239,233,233,.13)) !important;
      box-shadow: 0 18px 54px rgba(0,0,0,.20) !important;
      backdrop-filter: blur(16px) !important;
    }
    .kpi, .rules-panel, .settings-panel, .profile-card, .slider-row, .table-wrap {
      background: linear-gradient(180deg, rgba(31,36,30,.88), rgba(18,18,18,.88)) !important;
      border: 1px solid var(--cf-line, rgba(239,233,233,.13)) !important;
      border-radius: var(--cf-radius, 22px) !important;
      color: var(--cf-text, #EFE9E9) !important;
      box-shadow: var(--cf-shadow, 0 24px 80px rgba(0,0,0,.34)) !important;
    }
    .tabs, .segmented {
      background: rgba(239,233,233,.065) !important;
      border: 1px solid var(--cf-line, rgba(239,233,233,.13)) !important;
      border-radius: 999px !important;
      padding: 4px !important;
    }
    .tabs button, .segmented button, .button {
      border-radius: 999px !important;
      font-weight: 900 !important;
    }
    .tabs button.active, .segmented button.active, .button.primary {
      background: linear-gradient(135deg, var(--cf-green, #88AF60), var(--cf-green2, #A6C977)) !important;
      color: #10120F !important;
      border-color: rgba(136,175,96,.70) !important;
    }
    .button:not(.primary) {
      background: rgba(239,233,233,.07) !important;
      border-color: var(--cf-line, rgba(239,233,233,.13)) !important;
      color: var(--cf-text, #EFE9E9) !important;
    }
    .search {
      background: rgba(9,11,9,.72) !important;
      color: var(--cf-text, #EFE9E9) !important;
      border: 1px solid var(--cf-line, rgba(239,233,233,.13)) !important;
      border-radius: 14px !important;
    }
    .kpis { grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)) !important; }
    .kpi span, .subtitle, .muted, .contact, .reasons, .rule-block p, .profile-card span { color: var(--cf-muted, #C3BFBE) !important; }
    .kpi strong, .name, h1, h2, h3 { color: var(--cf-text, #EFE9E9) !important; }
    .table-wrap { overflow-x: auto !important; position: relative; }
    table { color: var(--cf-text, #EFE9E9) !important; border-collapse: separate !important; border-spacing: 0 !important; }
    th {
      background: rgba(136,175,96,.10) !important;
      color: var(--cf-text, #EFE9E9) !important;
      border-color: rgba(239,233,233,.10) !important;
    }
    td { color: var(--cf-text, #EFE9E9) !important; border-color: rgba(239,233,233,.10) !important; }
    tbody tr:nth-child(even) td { background: rgba(239,233,233,.025) !important; }
    tr:hover td { background: rgba(136,175,96,.055) !important; }
    .risk { border-radius: 999px !important; font-weight: 950 !important; }
    .risk.Alto, .risk.Vencido { background: rgba(227,107,93,.13) !important; color: #FFB1A8 !important; border: 1px solid rgba(227,107,93,.28) !important; }
    .risk.Medio, .risk.Hoy { background: rgba(230,179,95,.14) !important; color: #FFD18A !important; border: 1px solid rgba(230,179,95,.28) !important; }
    .risk.Bajo, .risk.Al.dia { background: rgba(136,175,96,.14) !important; color: var(--cf-green2, #A6C977) !important; border: 1px solid rgba(136,175,96,.28) !important; }
    .rule-block { background: rgba(239,233,233,.035) !important; border-color: rgba(239,233,233,.10) !important; }
    @media (max-width: 760px) {
      .shell { width: calc(100% - 20px) !important; }
      .main { padding-bottom: 18px !important; }
      .topbar { border-radius: 18px !important; }
      .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
      .tabs { width: 100%; display: grid; grid-template-columns: repeat(4, 1fr); border-radius: 18px !important; }
      .tabs button { padding: 0 8px; }
      .segmented { display: grid; grid-template-columns: repeat(4, 1fr); border-radius: 18px !important; }
      table { min-width: 780px !important; table-layout: auto !important; }
    }



    /* CFMP unified polish — aire, acento verde y lectura */
    .topbar { padding: 22px !important; }
    .kpi { position: relative; overflow: hidden; }
    .kpi::before { content: ""; position: absolute; left: 16px; right: 16px; top: 0; height: 3px; border-radius: 999px; background: linear-gradient(90deg, var(--cf-green, #88AF60), transparent); opacity: .9; }
    .kpi span { color: var(--cf-green2, #A6C977) !important; letter-spacing: .13em !important; }
    .subtitle, .muted, .contact, .reasons { color: rgba(239,233,233,.76) !important; }
    .toolbar { gap: 12px !important; margin-bottom: 14px !important; }
    .search { min-height: 46px !important; }
    th, td { padding: 12px 12px !important; }
    th:first-child, td:first-child { padding-left: 18px !important; }
    th:last-child, td:last-child { padding-right: 18px !important; }
    table { font-size: 12.5px !important; }
    .contact { line-height: 1.55 !important; }
    .reasons { line-height: 1.5 !important; }
    .risk { min-height: 26px !important; padding: 0 10px !important; }

  </style>
</head>
<body>
  
<style id="client-profile-overrides">
  body .client-link{appearance:none;border:0;background:transparent;color:var(--cf-text,#EFE9E9)!important;font:inherit;font-weight:900;text-align:left;padding:0;cursor:pointer;text-decoration:underline;text-decoration-color:rgba(136,175,96,.45);text-underline-offset:3px}
  body .client-link:hover{color:var(--cf-green2,#A6C977)!important}
  body .profile-modal{position:fixed;inset:0;z-index:2147483200;display:grid;place-items:center;padding:18px;background:rgba(0,0,0,.62);backdrop-filter:blur(10px)}
  body .profile-modal[hidden]{display:none!important}
  body .profile-card-modal{width:min(920px,100%);max-height:min(860px,92vh);overflow:auto;background:linear-gradient(180deg,rgba(31,36,30,.98),rgba(18,18,18,.98));border:1px solid var(--cf-line);border-radius:24px;box-shadow:var(--cf-shadow);padding:22px;color:var(--cf-text)}
  body .profile-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:14px}
  body .profile-close{min-width:42px!important;padding:0 12px!important}
  body .profile-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}
  body .profile-mini{border:1px solid var(--cf-line);border-radius:16px;padding:12px;background:rgba(239,233,233,.045)}
  body .profile-mini span{display:block;color:var(--cf-muted);font-size:11px;text-transform:uppercase;letter-spacing:.12em;font-weight:900;margin-bottom:4px}
  body .profile-note-box{display:grid;gap:10px;margin:14px 0}
  body .profile-note-box textarea{min-height:92px;background:rgba(9,11,9,.72);color:var(--cf-text);border:1px solid var(--cf-line);border-radius:16px;padding:12px;font:inherit}
  body .profile-injury-edit{display:block;margin:14px 0;padding:0;border:1px solid rgba(136,175,96,.24);border-radius:18px;background:rgba(136,175,96,.06);overflow:hidden}
  body .profile-injury-edit[hidden]{display:none!important}
  body .profile-injury-edit summary{list-style:none;cursor:pointer;user-select:none;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:14px 16px;color:var(--cf-text);font-weight:950}
  body .profile-injury-edit summary::-webkit-details-marker{display:none}
  body .profile-injury-edit summary::after{content:"⌄";color:var(--cf-green2,#A6C977);font-size:15px}
  body .profile-injury-edit[open] summary::after{content:"⌃"}
  body .profile-injury-edit-body{display:grid;gap:10px;padding:0 14px 14px}
  body .profile-injury-grid{display:grid;grid-template-columns:minmax(180px,.55fr) minmax(0,1fr);gap:10px;align-items:end}
  body .profile-injury-edit label{display:grid;gap:6px;color:var(--cf-muted);font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.08em}
  body .profile-injury-edit select,body .profile-injury-edit textarea{width:100%;box-sizing:border-box;background:rgba(9,11,9,.72);color:var(--cf-text);border:1px solid var(--cf-line);border-radius:14px;padding:11px;font:inherit;text-transform:none;letter-spacing:0;font-weight:500}
  body .profile-injury-edit textarea{min-height:84px;resize:vertical}
  body .profile-list{display:grid;gap:8px;margin-top:10px}
  body .profile-item{border:1px solid rgba(239,233,233,.10);border-radius:14px;padding:10px;background:rgba(239,233,233,.035);white-space:pre-wrap;overflow-wrap:anywhere}
  body .profile-item small{display:block;color:var(--cf-muted);margin-top:4px}
  @media(max-width:640px){body .profile-modal{padding:0;place-items:stretch}body .profile-grid,body .profile-injury-grid{grid-template-columns:1fr}body .profile-card-modal{width:100%;max-height:100dvh;height:100dvh;padding:16px;border-radius:0}body .profile-note-box textarea,body .profile-injury-edit textarea,body .profile-injury-edit select{font-size:16px}body .profile-note-box textarea{min-height:132px}body .profile-item{font-size:15px;line-height:1.38}}
</style>
  
<style id="risk-lesionados-app-overrides">
  body .injury-toolbar{grid-template-columns:minmax(260px,1fr) auto auto auto!important}
  body #injuriesPanel table{table-layout:fixed!important}
  body #injuriesPanel th:nth-child(1),body #injuriesPanel td:nth-child(1){width:10%!important}
  body #injuriesPanel th:nth-child(2),body #injuriesPanel td:nth-child(2){width:13%!important}
  body #injuriesPanel th:nth-child(3),body #injuriesPanel td:nth-child(3){width:17%!important}
  body #injuriesPanel th:nth-child(4),body #injuriesPanel td:nth-child(4){width:7%!important}
  body #injuriesPanel th:nth-child(5),body #injuriesPanel td:nth-child(5){width:23%!important}
  body #injuriesPanel th:nth-child(6),body #injuriesPanel td:nth-child(6){width:11%!important}
  body #injuriesPanel th:nth-child(7),body #injuriesPanel td:nth-child(7){width:9%!important}
  body #injuriesPanel th:nth-child(8),body #injuriesPanel td:nth-child(8){width:8%!important}
  @media(max-width:980px){body .injury-toolbar{grid-template-columns:1fr!important}body #injuriesPanel table{min-width:980px!important;table-layout:fixed!important}}
</style>
  <style id="risk-final-unified-overrides">
  body .shell{display:block!important;width:min(calc(100% - 32px),1440px)!important;max-width:1440px!important;margin:0 auto!important;min-height:auto!important}
  body .shell aside{display:none!important}
  body .main{width:100%!important;max-width:1440px!important;margin:0 auto!important;padding:0 0 28px!important}
  body .topbar{position:static!important;top:auto!important;background:linear-gradient(180deg,rgba(31,36,30,.78),rgba(18,18,18,.78))!important;border:1px solid var(--cf-line)!important;border-radius:var(--cf-radius)!important;padding:22px!important;margin:0 0 18px!important;box-shadow:0 18px 54px rgba(0,0,0,.22)!important;backdrop-filter:blur(16px)!important}
  body .tabs,body .segmented{display:inline-flex!important;gap:4px!important;background:rgba(239,233,233,.065)!important;border:1px solid var(--cf-line)!important;border-radius:999px!important;padding:4px!important;box-shadow:none!important}
  body .tabs button,body .segmented button{background:transparent!important;color:var(--cf-text)!important;border:0!important;border-radius:999px!important;box-shadow:none!important;min-height:36px!important;padding:0 14px!important;font-weight:900!important;white-space:nowrap!important}
  body .tabs button.active,body .segmented button.active{background:linear-gradient(135deg,var(--cf-green),var(--cf-green2))!important;color:#10120F!important;border-color:rgba(136,175,96,.70)!important;box-shadow:0 10px 24px rgba(136,175,96,.16)!important}
  body .toolbar{display:grid!important;grid-template-columns:minmax(260px,1fr) auto auto!important;gap:12px!important;align-items:center!important;margin-bottom:14px!important}
  body .search{height:46px!important;background:rgba(9,11,9,.72)!important;color:var(--cf-text)!important;border:1px solid var(--cf-line)!important;border-radius:14px!important}
  body .table-wrap{overflow-x:auto!important;background:linear-gradient(180deg,rgba(31,36,30,.88),rgba(18,18,18,.88))!important;border:1px solid var(--cf-line)!important;border-radius:var(--cf-radius)!important;box-shadow:var(--cf-shadow)!important}
  body table{width:100%!important;min-width:0!important;table-layout:fixed!important;font-size:12.5px!important;color:var(--cf-text)!important}
  body th,body td{padding:12px!important;color:var(--cf-text)!important;border-color:rgba(239,233,233,.10)!important;overflow-wrap:anywhere!important}
  body th{background:rgba(136,175,96,.10)!important;line-height:1.15!important;white-space:normal!important;vertical-align:bottom!important}
  body th button{display:block!important;background:transparent!important;color:var(--cf-text)!important;border:0!important;box-shadow:none!important;padding:0!important;min-height:auto!important;line-height:1.15!important;white-space:normal!important;text-align:left!important}
  body th button::after{content:""!important}
  body th:first-child,body td:first-child{padding-left:18px!important}
  body th:nth-child(1),body td:nth-child(1){width:8%!important}body th:nth-child(2),body td:nth-child(2){width:18%!important}body th:nth-child(3),body td:nth-child(3){width:18%!important}body th:nth-child(4),body td:nth-child(4){width:11%!important}body th:nth-child(5),body td:nth-child(5){width:15%!important}body th:nth-child(6),body td:nth-child(6){width:12%!important}body th:nth-child(7),body td:nth-child(7){width:18%!important}
  body .risk{border-radius:999px!important;font-weight:950!important;min-height:26px!important;padding:0 10px!important;width:max-content!important;max-width:100%!important}
  @media(max-width:760px){body .shell{width:calc(100% - 20px)!important}body .toolbar{grid-template-columns:1fr!important}body .tabs,body .segmented{display:grid!important;grid-template-columns:repeat(4,1fr)!important;border-radius:18px!important;width:100%!important}body table{min-width:780px!important;table-layout:auto!important}}
</style>
<style id="sortable-list-overrides">
  body th button[data-sort]::after,body th button[data-injury-sort]::after,body th button[data-pending-sort]::after,body th button[data-deleted-sort]::after{content:"↕"!important;color:#90a18a!important;font-size:12px!important;float:right!important;margin-left:6px!important}
  body th button.active.asc::after{content:"↑"!important;color:var(--cf-green)!important}
  body th button.active.desc::after{content:"↓"!important;color:var(--cf-green)!important}
  body #pendingPanel .client-link,body #injuriesPanel .client-link,body #deletedPanel .client-link{background:transparent!important;border:0!important;border-radius:0!important;box-shadow:none!important;padding:0!important;min-height:0!important;color:var(--cf-text,#EFE9E9)!important;text-decoration:none!important;display:inline!important;font-weight:900!important;line-height:1.25!important}
  body #pendingPanel .client-link:hover,body #injuriesPanel .client-link:hover,body #deletedPanel .client-link:hover{color:var(--cf-green2,#A6C977)!important;background:transparent!important}
  body .workspace-nav{display:grid!important;grid-template-columns:1fr 1fr!important;gap:14px!important;margin:0 0 18px!important}
  body .nav-group{border:1px solid var(--cf-line)!important;border-radius:22px!important;background:linear-gradient(180deg,rgba(31,36,30,.72),rgba(18,18,18,.72))!important;padding:14px!important;box-shadow:0 16px 42px rgba(0,0,0,.16)!important}
  body .nav-group-label{font-size:11px!important;letter-spacing:.14em!important;text-transform:uppercase!important;color:var(--cf-muted)!important;font-weight:950!important;margin:0 0 10px!important}
  body .risk-group{border-color:rgba(136,175,96,.24)!important}
  body .injury-group{border-color:rgba(240,184,90,.24)!important}
  body .panel-head{display:flex!important;justify-content:space-between!important;gap:18px!important;align-items:flex-end!important;margin:0 0 14px!important;padding:16px!important;border:1px solid var(--cf-line)!important;border-radius:20px!important;background:rgba(239,233,233,.035)!important}
  body .panel-head span{display:block!important;font-size:11px!important;letter-spacing:.14em!important;text-transform:uppercase!important;color:var(--cf-muted)!important;font-weight:950!important;margin-bottom:5px!important}
  body .panel-head h2{margin:0!important;font-size:22px!important;line-height:1.05!important;letter-spacing:-.03em!important;color:var(--cf-text)!important}
  body .panel-head p{max-width:560px!important;margin:0!important;color:var(--cf-muted)!important;font-size:13px!important;line-height:1.4!important}
  body .risk-head{border-color:rgba(136,175,96,.22)!important}
  body .injury-head{border-color:rgba(240,184,90,.22)!important}
  body .toolbar .button{min-height:40px!important;white-space:nowrap!important}
  body .toolbar{grid-template-columns:minmax(260px,1fr) auto auto auto!important}
  @media(max-width:900px){body .workspace-nav{grid-template-columns:1fr!important}body .panel-head{align-items:flex-start!important;flex-direction:column!important}body .toolbar{grid-template-columns:1fr!important}}
</style>
<style id="injury-list-compact-overrides">
  /* Compactar el listado completo de lesionados: menos alto de fila y columnas proporcionadas */
  body #injuriesPanel .table-wrap{overflow-x:auto!important}
  body #injuriesPanel table{min-width:1120px!important;table-layout:fixed!important;font-size:12px!important}
  body #injuriesPanel th,body #injuriesPanel td{padding:7px 9px!important;vertical-align:middle!important;line-height:1.2!important}
  body #injuriesPanel th:nth-child(1),body #injuriesPanel td:nth-child(1){width:8%!important}
  body #injuriesPanel th:nth-child(2),body #injuriesPanel td:nth-child(2){width:12%!important}
  body #injuriesPanel th:nth-child(3),body #injuriesPanel td:nth-child(3){width:16%!important}
  body #injuriesPanel th:nth-child(4),body #injuriesPanel td:nth-child(4){width:7%!important}
  body #injuriesPanel th:nth-child(5),body #injuriesPanel td:nth-child(5){width:14%!important}
  body #injuriesPanel th:nth-child(6),body #injuriesPanel td:nth-child(6){width:10%!important}
  body #injuriesPanel th:nth-child(7),body #injuriesPanel td:nth-child(7){width:11%!important}
  body #injuriesPanel th:nth-child(8),body #injuriesPanel td:nth-child(8){width:11%!important}
  body #injuriesPanel th:nth-child(9),body #injuriesPanel td:nth-child(9){width:13%!important}
  body #injuriesPanel .muted,body #injuriesPanel .contact{font-size:11px!important;line-height:1.15!important;margin-top:2px!important}
  body #injuriesPanel .name{font-weight:850!important;line-height:1.18!important}
  body #injuriesPanel td:nth-child(5) .muted,body #injuriesPanel td:nth-child(7) .muted{display:-webkit-box!important;-webkit-line-clamp:2!important;-webkit-box-orient:vertical!important;overflow:hidden!important;max-height:2.5em!important}
  body #injuriesPanel .reasons{white-space:normal!important;overflow:visible!important;max-height:none!important;line-height:1.25!important}
  body #injuriesPanel .risk{min-height:22px!important;padding:0 8px!important;font-size:11px!important;line-height:22px!important}
  body #injuriesPanel .button{min-height:30px!important;padding:0 8px!important;font-size:11px!important;white-space:normal!important;line-height:1.1!important;margin:2px 0!important}
  body .risk.Pendiente.respuesta{background:rgba(240,184,90,.16)!important;color:#F0C879!important;border:1px solid rgba(240,184,90,.42)!important}
  @media(max-width:760px){body #injuriesPanel table{min-width:980px!important}}
</style>

<style id="lesionados-mobile-perfect-overrides">
  @media(max-width:760px){
    html,body{overflow-x:hidden!important}
    body{padding-top:calc(12px + env(safe-area-inset-top,0px))!important}
    body .shell{width:calc(100% - 20px)!important;max-width:none!important;margin:0 auto!important}
    body .main{padding-bottom:22px!important}
    body .topbar{padding:16px!important;border-radius:20px!important;margin-bottom:14px!important;gap:14px!important}
    body .topbar h1{font-size:26px!important;line-height:1.02!important;letter-spacing:-.04em!important;margin:0 0 6px!important}
    body .subtitle{font-size:12px!important;line-height:1.3!important;word-break:break-word!important}
    body .actions{display:grid!important;grid-template-columns:1fr 1fr!important;gap:8px!important;width:100%!important}
    body .actions .button{min-height:38px!important;padding:0 10px!important;font-size:12px!important;justify-content:center!important}
    body .actions .button:first-child{grid-column:1/-1!important}
    body .kpis{grid-template-columns:1fr 1fr!important;gap:10px!important}
    body .kpi{padding:12px!important;border-radius:18px!important;min-height:92px!important}
    body .kpi strong{font-size:24px!important}
    body .workspace-nav{gap:10px!important}
    body .nav-group{padding:10px!important;border-radius:18px!important;overflow:hidden!important}
    body .tabs{display:flex!important;overflow-x:auto!important;gap:6px!important;width:100%!important;border-radius:18px!important;scroll-snap-type:x proximity!important;-webkit-overflow-scrolling:touch!important}
    body .tabs button{flex:0 0 auto!important;min-height:34px!important;padding:0 11px!important;font-size:11px!important;scroll-snap-align:start!important}
    body .injury-toolbar,body #injuriesPanel .toolbar,body #pendingPanel .toolbar{display:grid!important;grid-template-columns:1fr!important;gap:9px!important;margin-bottom:12px!important}
    body #injuriesPanel .segmented,body #pendingPanel .segmented{display:flex!important;overflow-x:auto!important;width:100%!important;border-radius:18px!important;-webkit-overflow-scrolling:touch!important}
    body #injuriesPanel .segmented button,body #pendingPanel .segmented button{flex:0 0 auto!important;font-size:11px!important;padding:0 10px!important;min-height:34px!important}
    body #injuriesPanel .search,body #pendingPanel .search{height:42px!important;border-radius:13px!important;font-size:14px!important}
    body .panel-head{padding:14px!important;border-radius:18px!important;margin-bottom:12px!important}
    body .panel-head h2{font-size:20px!important}
    body .panel-head p{font-size:12px!important}

    /* Lesionados y pendientes como tarjetas móviles, no tabla comprimida */
    body #injuriesPanel .table-wrap,body #pendingPanel .table-wrap,body #deletedPanel .table-wrap{overflow:visible!important;background:transparent!important;border:0!important;box-shadow:none!important;border-radius:0!important}
    body #injuriesPanel table,body #pendingPanel table,body #deletedPanel table{display:block!important;width:100%!important;min-width:0!important;table-layout:auto!important;border-collapse:separate!important;border-spacing:0!important;background:transparent!important}
    body #injuriesPanel thead,body #pendingPanel thead,body #deletedPanel thead{display:none!important}
    body #injuriesPanel tbody,body #pendingPanel tbody,body #deletedPanel tbody{display:grid!important;gap:12px!important;width:100%!important}
    body #injuriesPanel tr,body #pendingPanel tr,body #deletedPanel tr{display:grid!important;gap:8px!important;width:100%!important;padding:13px!important;border:1px solid rgba(239,233,233,.12)!important;border-radius:18px!important;background:linear-gradient(180deg,rgba(31,36,30,.88),rgba(18,18,18,.90))!important;box-shadow:0 12px 32px rgba(0,0,0,.18)!important}
    body #injuriesPanel td,body #pendingPanel td,body #deletedPanel td{display:grid!important;grid-template-columns:92px minmax(0,1fr)!important;gap:10px!important;align-items:start!important;width:100%!important;padding:0!important;border:0!important;min-height:0!important;font-size:13px!important;line-height:1.25!important;overflow:visible!important;max-height:none!important;white-space:normal!important}
    body #injuriesPanel td::before,body #pendingPanel td::before,body #deletedPanel td::before{font-size:10px!important;line-height:1.15!important;letter-spacing:.11em!important;text-transform:uppercase!important;font-weight:950!important;color:rgba(195,191,190,.78)!important;padding-top:4px!important}
    body #injuriesPanel td:nth-child(1)::before,body #pendingPanel td:nth-child(2)::before,body #deletedPanel td:nth-child(2)::before{content:"Centro"}
    body #injuriesPanel td:nth-child(2)::before{content:"Seguimiento"}
    body #injuriesPanel td:nth-child(3)::before,body #pendingPanel td:nth-child(3)::before,body #deletedPanel td:nth-child(3)::before{content:"Socio"}
    body #injuriesPanel td:nth-child(4)::before{content:"Tipo"}
    body #injuriesPanel td:nth-child(5)::before{content:"Etiqueta"}
    body #injuriesPanel td:nth-child(6)::before,body #pendingPanel td:nth-child(4)::before,body #deletedPanel td:nth-child(4)::before{content:"Lesión"}
    body #injuriesPanel td:nth-child(7)::before,body #pendingPanel td:nth-child(5)::before{content:"Próximo"}
    body #injuriesPanel td:nth-child(8)::before,body #pendingPanel td:nth-child(6)::before{content:"Última acción"}
    body #injuriesPanel td:nth-child(9)::before,body #pendingPanel td:nth-child(7)::before{content:"Acciones"}
    body #pendingPanel td:nth-child(1)::before{content:"Vence"}
    body #deletedPanel td:nth-child(1)::before{content:"Curado"}
    body #deletedPanel td:nth-child(5)::before{content:"Historial"}
    body #injuriesPanel .risk,body #pendingPanel .risk,body #deletedPanel .risk{width:max-content!important;max-width:100%!important;white-space:normal!important;min-height:24px!important;line-height:22px!important;padding:1px 9px!important;font-size:11px!important}
    body #injuriesPanel .contact,body #pendingPanel .contact,body #deletedPanel .contact{font-size:12px!important;color:rgba(195,191,190,.88)!important;margin-top:3px!important}
    body #injuriesPanel .muted,body #pendingPanel .muted,body #deletedPanel .muted{font-size:12px!important;color:rgba(195,191,190,.88)!important;line-height:1.25!important}
    body #injuriesPanel .name,body #pendingPanel .name,body #deletedPanel .name{font-size:13px!important;line-height:1.25!important}
    body #injuriesPanel .reasons,body #pendingPanel .reasons,body #deletedPanel .reasons{font-size:12px!important;line-height:1.28!important;color:rgba(239,233,233,.92)!important;overflow:visible!important;max-height:none!important;display:block!important}
    body #injuriesPanel .client-link,body #pendingPanel .client-link,body #deletedPanel .client-link{font-size:14px!important;line-height:1.2!important;color:var(--cf-text,#EFE9E9)!important}
    body #injuriesPanel td:last-child,body #pendingPanel td:last-child{grid-template-columns:1fr!important;gap:8px!important;padding-top:4px!important;border-top:1px solid rgba(239,233,233,.08)!important}
    body #injuriesPanel td:last-child::before,body #pendingPanel td:last-child::before{padding-top:0!important}
    body #injuriesPanel td:last-child,body #pendingPanel td:last-child{display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr))!important}
    body #injuriesPanel td:last-child::before,body #pendingPanel td:last-child::before{grid-column:1/-1!important}
    body #injuriesPanel td:last-child .button,body #pendingPanel td:last-child .button{width:100%!important;min-height:36px!important;margin:0!important;padding:0 8px!important;font-size:11px!important;border-radius:12px!important;line-height:1.05!important;justify-content:center!important}
    body #injuriesPanel td:last-child .followup-remove,body #pendingPanel td:last-child .followup-remove{grid-column:1/-1!important}
    body #injuryCountLabel,body #pendingCountLabel,body #deletedCountLabel{justify-self:start!important;font-size:12px!important;padding-left:2px!important}
  }
</style>

<style id="lesionados-mobile-width-hotfix">
  @media(max-width:760px){
    body #injuriesPanel th,body #injuriesPanel td,
    body #pendingPanel th,body #pendingPanel td,
    body #deletedPanel th,body #deletedPanel td,
    body #injuriesPanel th:nth-child(n),body #injuriesPanel td:nth-child(n),
    body #pendingPanel th:nth-child(n),body #pendingPanel td:nth-child(n),
    body #deletedPanel th:nth-child(n),body #deletedPanel td:nth-child(n){width:100%!important;max-width:100%!important;min-width:0!important}
    body #injuriesPanel td,body #pendingPanel td,body #deletedPanel td{display:grid!important;grid-template-columns:1fr!important;gap:4px!important;overflow-wrap:normal!important;word-break:normal!important;overflow:visible!important;max-height:none!important}
    body #injuriesPanel td::before,body #pendingPanel td::before,body #deletedPanel td::before{display:block!important;padding-top:0!important;margin-bottom:1px!important}
    body #injuriesPanel td > *,body #pendingPanel td > *,body #deletedPanel td > *{min-width:0!important;max-width:100%!important;overflow-wrap:break-word!important;word-break:normal!important}
    body #injuriesPanel tr,body #pendingPanel tr,body #deletedPanel tr{gap:10px!important}
    body #injuriesPanel td:nth-child(1),body #injuriesPanel td:nth-child(2),body #pendingPanel td:nth-child(1),body #pendingPanel td:nth-child(2){display:flex!important;align-items:center!important;justify-content:space-between!important;gap:10px!important}
    body #injuriesPanel td:nth-child(1)::before,body #injuriesPanel td:nth-child(2)::before,body #pendingPanel td:nth-child(1)::before,body #pendingPanel td:nth-child(2)::before{margin:0!important;flex:0 0 auto!important}
    body #injuriesPanel td:last-child,body #pendingPanel td:last-child{display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:8px!important;width:100%!important}
    body #injuriesPanel td:last-child::before,body #pendingPanel td:last-child::before{grid-column:1/-1!important}
    body #injuriesPanel .risk,body #pendingPanel .risk{white-space:nowrap!important;max-width:100%!important}
    body #injuriesPanel .risk.Bajo{white-space:normal!important}
  }
</style>

<style id="lesionados-mobile-filter-final-fix">
  @media(max-width:760px){
    body #injuriesPanel .segmented,body #pendingPanel .segmented{display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:6px!important;overflow:visible!important;padding:5px!important;border-radius:16px!important}
    body #injuriesPanel .segmented button,body #pendingPanel .segmented button{width:100%!important;min-width:0!important;flex:none!important;white-space:normal!important;line-height:1.05!important;min-height:36px!important;text-align:center!important}
    body .tabs button.active,body .segmented button.active,
    body #injuriesPanel .segmented button.active,body #pendingPanel .segmented button.active{background:linear-gradient(135deg,var(--cf-green,#88AF60),var(--cf-green2,#A6C977))!important;color:#10120F!important;text-shadow:none!important;border-color:rgba(166,201,119,.65)!important}
    body .tabs button:not(.active),body .segmented button:not(.active){color:rgba(239,233,233,.92)!important}
    body #injuriesPanel .injury-toolbar,body #pendingPanel .injury-toolbar{gap:10px!important}
  }
</style>

<style id="mobile-placeholder-contrast">
  @media(max-width:760px){body .search::placeholder{color:rgba(239,233,233,.72)!important;opacity:1!important}}
</style>

<style id="lesionados-mobile-collapsible-contacts">
  @media(max-width:760px){
    body #injuriesPanel tbody tr{cursor:pointer!important;position:relative!important}
    body #injuriesPanel tbody tr:not(.expanded) td:nth-child(4),
    body #injuriesPanel tbody tr:not(.expanded) td:nth-child(7),
    body #injuriesPanel tbody tr:not(.expanded) td:nth-child(8){display:none!important}
    body #injuriesPanel tbody tr::after{display:none!important;content:""!important}
    body #injuriesPanel tbody tr td:nth-child(9){order:20!important}
  }
</style>

<style id="lesionados-mobile-more-compact-collapse">
  @media(max-width:760px){
    body #injuriesPanel tbody tr:not(.expanded) td:nth-child(5) .muted{display:none!important}
    body #injuriesPanel tbody tr:not(.expanded){gap:8px!important;padding:12px!important}
    body #injuriesPanel tbody tr::after{display:none!important;content:""!important}
  }
</style>

<style id="injury-note-button-style">
  body .followup-note{border-color:rgba(136,175,96,.42)!important;color:#A6C977!important}
</style>
<style id="injury-action-menu-style">
  body .action-menu-trigger{display:flex!important;align-items:center!important;justify-content:center!important;gap:8px!important;width:100%!important;min-height:38px!important;padding:0 14px!important;border:1px solid rgba(136,175,96,.62)!important;border-radius:14px!important;background:#22301D!important;color:#EAF4E2!important;font-weight:900!important;box-shadow:0 10px 24px rgba(0,0,0,.26)!important;cursor:pointer!important}
  body .action-menu-trigger::after{content:"⌄";font-size:14px;color:#A6C977!important;margin-left:2px!important}
  body .action-menu-trigger[aria-expanded="true"]{background:#2A3A22!important;border-color:rgba(166,201,119,.78)!important}
  body .action-menu-trigger[aria-expanded="true"]::after{content:"⌃"}
  body #globalActionMenu{position:fixed!important;display:none!important;grid-template-columns:1fr!important;gap:8px!important;width:min(250px,calc(100vw - 24px))!important;padding:12px!important;border:1px solid rgba(136,175,96,.48)!important;border-radius:16px!important;background:#0F120E!important;box-shadow:0 24px 60px rgba(0,0,0,.78),0 0 0 9999px rgba(0,0,0,.08)!important;z-index:2147483000!important;box-sizing:border-box!important}
  body #globalActionMenu.open{display:grid!important}
  body #globalActionMenu .button{width:100%!important;justify-content:flex-start!important;min-height:40px!important;white-space:nowrap!important;background:#1B2118!important;border-color:rgba(239,233,233,.16)!important;color:#F3F7EF!important}
  body #globalActionMenu .button.primary{background:#88AF60!important;border-color:#88AF60!important;color:#101510!important}
  body #globalActionMenu .button:hover{background:#26311F!important;border-color:rgba(136,175,96,.65)!important}
  @media(max-width:760px){body .action-menu-trigger{min-height:42px!important}body #globalActionMenu{position:fixed!important;left:12px!important;right:12px!important;bottom:12px!important;top:auto!important;width:auto!important;max-height:calc(100vh - 24px)!important;overflow:auto!important;box-shadow:0 24px 60px rgba(0,0,0,.78),0 0 0 9999px rgba(0,0,0,.35)!important}body #globalActionMenu .button{justify-content:center!important;min-height:44px!important}}
</style>
<style id="remove-mobile-slide-control-final">
  @media(max-width:760px){
    body #injuriesPanel tbody tr{cursor:default!important}
    body #injuriesPanel tbody tr::after,body #injuriesPanel tbody tr::before{display:none!important;content:""!important;width:0!important;height:0!important;padding:0!important;border:0!important;margin:0!important;background:transparent!important}
    body #injuriesPanel tbody tr:not(.expanded) td:nth-child(4),body #injuriesPanel tbody tr:not(.expanded) td:nth-child(5),body #injuriesPanel tbody tr:not(.expanded) td:nth-child(6),body #injuriesPanel tbody tr:not(.expanded) td:nth-child(7),body #injuriesPanel tbody tr:not(.expanded) td:nth-child(8),body #injuriesPanel tbody tr:not(.expanded) td:nth-child(9),body #injuriesPanel tbody tr td:nth-child(4),body #injuriesPanel tbody tr td:nth-child(5),body #injuriesPanel tbody tr td:nth-child(6),body #injuriesPanel tbody tr td:nth-child(7),body #injuriesPanel tbody tr td:nth-child(8),body #injuriesPanel tbody tr td:nth-child(9){display:grid!important}
    body #injuriesPanel tbody tr td:nth-child(1),body #injuriesPanel tbody tr td:nth-child(2){display:flex!important}
    body #injuriesPanel tbody tr:not(.expanded) td:nth-child(5) .muted,body #injuriesPanel tbody tr td:nth-child(5) .muted{display:block!important}
  }
</style>

<style id="injuries-no-origin-column-widths">
  body #injuriesPanel table{min-width:1120px!important}
  body #injuriesPanel th:nth-child(1),body #injuriesPanel td:nth-child(1){width:8%!important}
  body #injuriesPanel th:nth-child(2),body #injuriesPanel td:nth-child(2){width:12%!important}
  body #injuriesPanel th:nth-child(3),body #injuriesPanel td:nth-child(3){width:17%!important}
  body #injuriesPanel th:nth-child(4),body #injuriesPanel td:nth-child(4){width:7%!important}
  body #injuriesPanel th:nth-child(5),body #injuriesPanel td:nth-child(5){width:12%!important}
  body #injuriesPanel th:nth-child(6),body #injuriesPanel td:nth-child(6){width:19%!important}
  body #injuriesPanel th:nth-child(7),body #injuriesPanel td:nth-child(7){width:11%!important}
  body #injuriesPanel th:nth-child(8),body #injuriesPanel td:nth-child(8){width:9%!important}
  body #injuriesPanel th:nth-child(9),body #injuriesPanel td:nth-child(9){width:5%!important}
</style>

<style id="desktop-compact-action-column-final">
  @media(min-width:761px){
    body #injuriesPanel table{min-width:1080px!important;table-layout:fixed!important}
    body #injuriesPanel th:nth-child(1),body #injuriesPanel td:nth-child(1){width:8%!important}
    body #injuriesPanel th:nth-child(2),body #injuriesPanel td:nth-child(2){width:12%!important}
    body #injuriesPanel th:nth-child(3),body #injuriesPanel td:nth-child(3){width:17%!important}
    body #injuriesPanel th:nth-child(4),body #injuriesPanel td:nth-child(4){width:6%!important}
    body #injuriesPanel th:nth-child(5),body #injuriesPanel td:nth-child(5){width:11%!important}
    body #injuriesPanel th:nth-child(6),body #injuriesPanel td:nth-child(6){width:22%!important}
    body #injuriesPanel th:nth-child(7),body #injuriesPanel td:nth-child(7){width:11%!important}
    body #injuriesPanel th:nth-child(8),body #injuriesPanel td:nth-child(8){width:9%!important}
    body #injuriesPanel th:nth-child(9),body #injuriesPanel td:nth-child(9){width:4%!important;min-width:58px!important;max-width:72px!important;padding-left:5px!important;padding-right:5px!important;text-align:center!important}
    body #injuriesPanel th:nth-child(9){font-size:0!important}
    body #injuriesPanel th:nth-child(9)::after{content:"⋯";font-size:16px!important;color:var(--cf-muted,#C3BFBE)!important}
    body #injuriesPanel .action-menu-trigger{min-width:0!important;width:42px!important;max-width:42px!important;height:32px!important;min-height:32px!important;padding:0!important;margin:0 auto!important;border-radius:11px!important;font-size:0!important;box-shadow:none!important}
    body #injuriesPanel .action-menu-trigger::before{content:"⋯";font-size:20px!important;line-height:1!important;color:#EAF4E2!important}
    body #injuriesPanel .action-menu-trigger::after{display:none!important;content:""!important}
    body #globalActionMenu{width:220px!important}
  }
</style>

<style id="injuries-fit-screen-no-action-20260529">
  @media(min-width:761px){
    body #injuriesPanel .table-wrap{overflow-x:hidden!important}
    body #injuriesPanel table{width:100%!important;min-width:0!important;table-layout:fixed!important;font-size:11.5px!important}
    body #injuriesPanel th,body #injuriesPanel td{padding-left:6px!important;padding-right:6px!important;vertical-align:top!important}
    body #injuriesPanel th button{line-height:1.12!important;white-space:normal!important;overflow:visible!important;text-overflow:clip!important}
    body #injuriesPanel th:nth-child(1),body #injuriesPanel td:nth-child(1){width:6%!important}
    body #injuriesPanel th:nth-child(2),body #injuriesPanel td:nth-child(2){width:11%!important}
    body #injuriesPanel th:nth-child(3),body #injuriesPanel td:nth-child(3){width:19%!important}
    body #injuriesPanel th:nth-child(4),body #injuriesPanel td:nth-child(4){width:10%!important}
    body #injuriesPanel th:nth-child(5),body #injuriesPanel td:nth-child(5){width:4%!important}
    body #injuriesPanel th:nth-child(6),body #injuriesPanel td:nth-child(6){width:5%!important}
    body #injuriesPanel th:nth-child(7),body #injuriesPanel td:nth-child(7){width:22%!important}
    body #injuriesPanel th:nth-child(8),body #injuriesPanel td:nth-child(8){width:8%!important}
    body #injuriesPanel th:nth-child(9),body #injuriesPanel td:nth-child(9){width:15%!important}
    body #injuriesPanel th:nth-child(9){font-size:inherit!important;text-align:left!important;white-space:normal!important}
    body #injuriesPanel th:nth-child(9)::after{display:none!important;content:""!important}
    body #injuriesPanel td:nth-child(6) .risk{display:none!important}
    body #injuriesPanel td:nth-child(6) .muted{display:block!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;max-width:100%!important}
    body #injuriesPanel td:nth-child(7) .name{display:-webkit-box!important;-webkit-line-clamp:2!important;-webkit-box-orient:vertical!important;overflow:hidden!important;white-space:normal!important}
    body #injuriesPanel td:nth-child(9).reasons{display:block!important;white-space:normal!important;overflow:visible!important;max-height:none!important;line-height:1.22!important}
    body #injuriesPanel td:nth-child(1),body #injuriesPanel td:nth-child(5),body #injuriesPanel td:nth-child(6),body #injuriesPanel td:nth-child(8){font-size:11px!important}
  }
  @media(max-width:760px){
    body #injuriesPanel td:nth-child(9)::before{content:"Última acción"!important;display:block!important}
  }
</style>

<style id="plain-table-headers-final">
  body th{background:rgba(18,18,18,.96)!important;color:rgba(239,233,233,.82)!important;border-bottom:1px solid rgba(239,233,233,.12)!important}
  body th button,body th button[data-sort],body th button[data-injury-sort],body th button[data-pending-sort],body th button[data-deleted-sort]{appearance:none!important;display:block!important;width:100%!important;min-height:0!important;height:auto!important;padding:0!important;margin:0!important;background:transparent!important;border:0!important;border-radius:0!important;box-shadow:none!important;color:inherit!important;font:inherit!important;font-weight:950!important;text-align:left!important;text-transform:uppercase!important;letter-spacing:.06em!important;line-height:1.15!important;white-space:normal!important;cursor:pointer!important}
  body th button::before,body th button::after,body th button[data-sort]::after,body th button[data-injury-sort]::after,body th button[data-pending-sort]::after,body th button[data-deleted-sort]::after,body th button.active.asc::after,body th button.active.desc::after{content:""!important;display:none!important}
  body th button:hover,body th button:focus-visible{color:var(--cf-green2,#A6C977)!important;outline:0!important;text-decoration:underline!important;text-underline-offset:3px!important;background:transparent!important;border:0!important;box-shadow:none!important}
</style>

<style id="remove-header-grey-box-final">
  body #injuriesPanel thead,body #injuriesPanel tr,body #injuriesPanel th{background:transparent!important;box-shadow:none!important}
  body #injuriesPanel th{border:0!important;border-bottom:1px solid rgba(239,233,233,.10)!important;color:rgba(239,233,233,.76)!important}
  body #injuriesPanel th button,body #injuriesPanel th button:hover,body #injuriesPanel th button:focus-visible{background:transparent!important;border:0!important;border-radius:0!important;box-shadow:none!important;text-decoration:none!important;outline:0!important}
</style>

<style id="injuries-plain-data-no-pills-20260526">
  body #injuriesPanel .risk,
  body #injuriesPanel .mobile-meta .risk,
  body #injuriesPanel td:nth-child(2) .risk{
    display:inline!important;
    width:auto!important;
    max-width:100%!important;
    min-height:0!important;
    height:auto!important;
    padding:0!important;
    margin:0!important;
    border:0!important;
    border-radius:0!important;
    background:transparent!important;
    box-shadow:none!important;
    line-height:inherit!important;
    font-size:inherit!important;
    white-space:normal!important;
  }
  body #injuriesPanel .risk.Bajo,
  body #injuriesPanel .risk.Al.dia{color:var(--cf-green2,#A6C977)!important}
  body #injuriesPanel .risk.Medio,
  body #injuriesPanel .risk.Hoy{color:#FFD18A!important}
  body #injuriesPanel .risk.Alto,
  body #injuriesPanel .risk.Vencido{color:#FFB1A8!important}
  body #injuriesPanel .risk.Proximos{color:#ABC8FF!important}
  body #injuriesPanel .risk.Sin.fecha{color:rgba(195,191,190,.86)!important}
  @media(max-width:760px){
    body #injuriesPanel .mobile-meta .risk{flex:0 0 auto!important;font-weight:950!important;font-size:10.8px!important;line-height:1.05!important}
  }
</style>


<style id="mobile-ux-review-20260525">
  @media(max-width:760px){
    /* Solo móvil: condensar jerarquía inicial sin tocar escritorio */
    html,body{max-width:100%!important;overflow-x:hidden!important}
    body{padding-top:max(8px,env(safe-area-inset-top,0px))!important;background:#090B09!important}
    body .shell{width:calc(100% - 14px)!important;margin:0 auto!important}
    body .main{padding:0 0 16px!important}

    body .topbar{display:grid!important;grid-template-columns:1fr auto!important;align-items:center!important;gap:10px!important;padding:11px 12px!important;margin-bottom:10px!important;border-radius:16px!important;box-shadow:0 10px 28px rgba(0,0,0,.22)!important;background:linear-gradient(180deg,rgba(31,36,30,.88),rgba(18,18,18,.92))!important}
    body .topbar h1{font-size:21px!important;line-height:1.02!important;margin:0 0 3px!important;letter-spacing:-.045em!important;white-space:normal!important}
    body .subtitle{font-size:10.5px!important;line-height:1.22!important;display:-webkit-box!important;-webkit-line-clamp:2!important;-webkit-box-orient:vertical!important;overflow:hidden!important;color:rgba(239,233,233,.68)!important}
    body .actions{display:flex!important;width:auto!important;gap:0!important;justify-content:flex-end!important}
    body .actions .button{width:auto!important;min-height:34px!important;min-width:112px!important;flex:0 0 auto!important;padding:0 12px!important;font-size:12px!important;border-radius:999px!important;box-shadow:none!important;white-space:nowrap!important}

    body .kpis{display:grid!important;grid-template-columns:repeat(4,minmax(0,1fr))!important;gap:6px!important;margin-bottom:10px!important}
    body .kpi{min-height:56px!important;padding:8px 6px!important;border-radius:14px!important;box-shadow:0 8px 22px rgba(0,0,0,.16)!important;text-align:center!important;background:rgba(31,36,30,.76)!important}
    body .kpi::before{left:8px!important;right:8px!important;height:2px!important;opacity:.55!important}
    body .kpi span{font-size:8px!important;line-height:1!important;letter-spacing:.08em!important;white-space:nowrap!important;color:rgba(166,201,119,.86)!important}
    body .kpi strong{font-size:20px!important;line-height:1!important;margin-top:6px!important}
    body .kpi small{display:none!important}

    body .workspace-nav{display:grid!important;grid-template-columns:1fr!important;gap:7px!important;margin-bottom:10px!important}
    body .nav-group{padding:8px!important;border-radius:15px!important;box-shadow:none!important;background:rgba(31,36,30,.58)!important}
    body .nav-group-label{font-size:9px!important;letter-spacing:.11em!important;margin-bottom:6px!important;color:rgba(195,191,190,.78)!important}
    body .tabs{display:flex!important;gap:5px!important;overflow-x:auto!important;scrollbar-width:none!important;border-radius:13px!important;padding:3px!important;background:rgba(239,233,233,.055)!important}
    body .tabs::-webkit-scrollbar,body .segmented::-webkit-scrollbar{display:none!important}
    body .tabs button{min-height:30px!important;padding:0 10px!important;font-size:10.5px!important;line-height:1!important;flex:0 0 auto!important}

    body .panel-head{padding:10px 11px!important;border-radius:15px!important;margin-bottom:9px!important;gap:5px!important;background:rgba(239,233,233,.028)!important}
    body .panel-head span{font-size:9px!important;margin-bottom:3px!important;letter-spacing:.11em!important}
    body .panel-head h2{font-size:18px!important;line-height:1.02!important}
    body .panel-head p{font-size:11px!important;line-height:1.25!important;display:-webkit-box!important;-webkit-line-clamp:2!important;-webkit-box-orient:vertical!important;overflow:hidden!important}

    body #injuriesPanel .injury-toolbar,body #pendingPanel .injury-toolbar,body #injuriesPanel .toolbar,body #pendingPanel .toolbar{gap:7px!important;margin-bottom:10px!important}
    body #injuriesPanel .search,body #pendingPanel .search{height:38px!important;min-height:38px!important;border-radius:12px!important;font-size:13px!important;padding:0 11px!important}
    body #injuriesPanel .segmented,body #pendingPanel .segmented{display:flex!important;overflow-x:auto!important;gap:5px!important;padding:3px!important;border-radius:13px!important;background:rgba(239,233,233,.055)!important}
    body #injuriesPanel .segmented button,body #pendingPanel .segmented button{min-height:30px!important;padding:0 10px!important;font-size:10.5px!important;line-height:1!important;white-space:nowrap!important;flex:0 0 auto!important;width:auto!important}
    body #injuryCountLabel,body #pendingCountLabel,body #deletedCountLabel{font-size:11px!important;color:rgba(239,233,233,.66)!important}

    /* Tarjetas de lesionados: cabecera clara + menos filas y menos ruido */
    body #injuriesPanel tbody,body #pendingPanel tbody,body #deletedPanel tbody{gap:8px!important}
    body #injuriesPanel tr,body #pendingPanel tr,body #deletedPanel tr{position:relative!important;display:grid!important;grid-template-columns:1fr auto!important;gap:5px 8px!important;padding:11px!important;border-radius:15px!important;background:linear-gradient(180deg,rgba(31,36,30,.84),rgba(15,18,14,.94))!important;box-shadow:0 10px 24px rgba(0,0,0,.18)!important}
    body #injuriesPanel td,body #pendingPanel td,body #deletedPanel td{display:block!important;width:auto!important;max-width:100%!important;min-width:0!important;padding:0!important;border:0!important;font-size:12px!important;line-height:1.22!important;color:rgba(239,233,233,.9)!important}
    body #injuriesPanel td::before,body #pendingPanel td::before,body #deletedPanel td::before{display:block!important;font-size:8.5px!important;line-height:1!important;margin:0 0 2px!important;letter-spacing:.09em!important;color:rgba(195,191,190,.66)!important}

    body #injuriesPanel td:nth-child(1){grid-column:1!important;grid-row:1!important;color:rgba(195,191,190,.9)!important;font-size:10.5px!important;text-transform:uppercase!important;font-weight:900!important;letter-spacing:.08em!important}
    body #injuriesPanel td:nth-child(1)::before{display:none!important}
    body #injuriesPanel td:nth-child(2){grid-column:2!important;grid-row:1!important;justify-self:end!important}
    body #injuriesPanel td:nth-child(2)::before{display:none!important}
    body #injuriesPanel td:nth-child(3){grid-column:1/-1!important;grid-row:2!important;padding-bottom:2px!important}
    body #injuriesPanel td:nth-child(3)::before{display:none!important}
    body #injuriesPanel td:nth-child(4){grid-column:1!important;grid-row:3!important}
    body #injuriesPanel td:nth-child(5){grid-column:1/-1!important;grid-row:4!important}
    body #injuriesPanel td:nth-child(6){grid-column:1/-1!important;grid-row:5!important}
    body #injuriesPanel td:nth-child(7){grid-column:1!important;grid-row:6!important}
    body #injuriesPanel td:nth-child(8){grid-column:1/-1!important;grid-row:7!important}
    body #injuriesPanel td:nth-child(9){grid-column:1/-1!important;grid-row:8!important;margin-top:3px!important;padding-top:7px!important;border-top:1px solid rgba(239,233,233,.08)!important;display:block!important}
    body #injuriesPanel td:nth-child(9)::before{content:""!important;display:none!important}

    body #injuriesPanel .client-link,body #pendingPanel .client-link,body #deletedPanel .client-link{font-size:15px!important;line-height:1.12!important;font-weight:950!important;text-decoration:none!important}
    body #injuriesPanel .contact,body #pendingPanel .contact,body #deletedPanel .contact{font-size:11.5px!important;line-height:1.2!important;margin-top:3px!important;color:rgba(195,191,190,.76)!important}
    body #injuriesPanel .name,body #pendingPanel .name,body #deletedPanel .name{font-size:14px!important;line-height:1.15!important;font-weight:950!important}
    body #injuriesPanel .muted,body #pendingPanel .muted,body #deletedPanel .muted{font-size:11.5px!important;line-height:1.22!important;color:rgba(195,191,190,.78)!important}
    body #injuriesPanel .reasons,body #pendingPanel .reasons,body #deletedPanel .reasons{font-size:12px!important;line-height:1.25!important;color:rgba(239,233,233,.88)!important;display:-webkit-box!important;-webkit-line-clamp:3!important;-webkit-box-orient:vertical!important;overflow:hidden!important}
    body #injuriesPanel td:nth-child(8) .reasons{font-size:11.5px!important;color:rgba(195,191,190,.86)!important;-webkit-line-clamp:2!important}
    body #injuriesPanel .risk,body #pendingPanel .risk,body #deletedPanel .risk{min-height:22px!important;line-height:20px!important;padding:1px 8px!important;font-size:10px!important;white-space:nowrap!important}
    body #injuriesPanel .action-menu-trigger,body #pendingPanel .action-menu-trigger{width:100%!important;min-height:36px!important;border-radius:12px!important;font-size:12px!important;background:#22301D!important;box-shadow:none!important}

    /* Riesgo de bajas en móvil: también tarjetas si se abre, no tabla horizontal */
    body #membersPanel .table-wrap{overflow:visible!important;background:transparent!important;border:0!important;box-shadow:none!important}
    body #membersPanel table{display:block!important;min-width:0!important;width:100%!important;background:transparent!important}
    body #membersPanel thead{display:none!important}
    body #membersPanel tbody{display:grid!important;gap:8px!important}
    body #membersPanel tr{display:grid!important;gap:6px!important;padding:11px!important;border:1px solid rgba(239,233,233,.12)!important;border-radius:15px!important;background:linear-gradient(180deg,rgba(31,36,30,.84),rgba(15,18,14,.94))!important;box-shadow:0 10px 24px rgba(0,0,0,.18)!important}
    body #membersPanel td{display:block!important;width:100%!important;padding:0!important;border:0!important;font-size:12px!important;line-height:1.25!important}
    body #membersPanel td::before{display:block!important;font-size:8.5px!important;letter-spacing:.09em!important;text-transform:uppercase!important;font-weight:950!important;color:rgba(195,191,190,.66)!important;margin-bottom:2px!important}
    body #membersPanel td:nth-child(1)::before{content:"Prioridad"}body #membersPanel td:nth-child(2)::before{content:"Socio"}body #membersPanel td:nth-child(3)::before{content:"Contacto"}body #membersPanel td:nth-child(4)::before{content:"Sin clase"}body #membersPanel td:nth-child(5)::before{content:"Último pago"}body #membersPanel td:nth-child(6)::before{content:"Tarifa"}body #membersPanel td:nth-child(7)::before{content:"Motivos"}
  }
</style>


<style id="mobile-ux-review-pass2-20260525">
  @media(max-width:760px){
    /* Pasada 2: que aparezcan resultados antes, sin afectar PC */
    body .topbar{grid-template-columns:1fr auto!important;padding:9px 10px!important;margin-bottom:8px!important;border-radius:14px!important;gap:8px!important}
    body .topbar h1{font-size:19px!important;line-height:1!important;margin-bottom:2px!important}
    body .subtitle{font-size:10px!important;line-height:1.15!important;-webkit-line-clamp:1!important;white-space:nowrap!important;text-overflow:ellipsis!important;display:block!important;overflow:hidden!important;max-width:100%!important}
    body .actions .button{min-height:31px!important;min-width:104px!important;padding:0 10px!important;font-size:11px!important}

    body .kpis{display:flex!important;grid-template-columns:none!important;gap:6px!important;overflow-x:auto!important;-webkit-overflow-scrolling:touch!important;scrollbar-width:none!important;margin-bottom:8px!important;padding-bottom:1px!important}
    body .kpis::-webkit-scrollbar{display:none!important}
    body .kpi{flex:0 0 74px!important;min-height:50px!important;padding:7px 5px!important;border-radius:12px!important;text-align:center!important}
    body .kpi span{font-size:7.5px!important;letter-spacing:.055em!important;display:block!important;overflow:hidden!important;text-overflow:ellipsis!important;white-space:nowrap!important}
    body .kpi strong{font-size:18px!important;margin-top:5px!important}

    body .workspace-nav{gap:6px!important;margin-bottom:8px!important}
    body .nav-group{padding:7px!important;border-radius:13px!important}
    body .nav-group-label{font-size:8.5px!important;margin-bottom:5px!important}
    body .tabs button{min-height:28px!important;font-size:10px!important;padding:0 9px!important}

    body .panel-head{padding:9px 10px!important;margin-bottom:8px!important;border-radius:13px!important}
    body .panel-head h2{font-size:17px!important}
    body .panel-head p{display:none!important}

    body #injuriesPanel .search,body #pendingPanel .search,body #membersPanel .search{height:38px!important;min-height:38px!important;background:rgba(9,11,9,.92)!important;border:1px solid rgba(166,201,119,.32)!important;box-shadow:inset 0 0 0 1px rgba(255,255,255,.025)!important}
    body #injuriesPanel .segmented,body #pendingPanel .segmented{gap:4px!important;padding:3px!important;margin-bottom:0!important}
    body #injuriesPanel .segmented button,body #pendingPanel .segmented button{min-height:29px!important;font-size:10px!important;padding:0 8px!important;max-width:none!important}
    body #injuriesPanel .injury-toolbar,body #pendingPanel .injury-toolbar,body #injuriesPanel .toolbar,body #pendingPanel .toolbar{gap:6px!important;margin-bottom:8px!important}

    body #injuriesPanel tr,body #pendingPanel tr,body #deletedPanel tr,body #membersPanel tr{border-radius:14px!important;padding:10px!important;gap:4px 8px!important}
    body #injuriesPanel tbody,body #pendingPanel tbody,body #deletedPanel tbody,body #membersPanel tbody{gap:7px!important}
    body #injuriesPanel .reasons,body #pendingPanel .reasons,body #deletedPanel .reasons{line-height:1.22!important;-webkit-line-clamp:2!important}
    body #injuriesPanel td:nth-child(6) .reasons{font-size:11.7px!important}
    body #injuriesPanel td:nth-child(8) .reasons{display:block!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important}
  }
</style>


<style id="mobile-ux-review-pass3-no-clipped-chips-20260525">
  @media(max-width:760px){
    /* No chips cortados: móvil sin sensación de overflow */
    body .kpis{display:grid!important;grid-template-columns:repeat(4,minmax(0,1fr))!important;overflow:visible!important;gap:6px!important;margin-bottom:8px!important;padding:0!important}
    body .kpi{flex:none!important;min-width:0!important;width:auto!important;min-height:50px!important}
    body .kpi:nth-child(1),body .kpi:nth-child(3),body .kpi:nth-child(4),body .kpi:nth-child(8){display:none!important}
    body .kpi span{font-size:7px!important;letter-spacing:.035em!important}
    body .kpi strong{font-size:17px!important}

    body #injuriesPanel .segmented,body #pendingPanel .segmented{display:flex!important;flex-wrap:wrap!important;overflow:visible!important;gap:5px!important;width:100%!important}
    body #injuriesPanel .segmented button,body #pendingPanel .segmented button{flex:1 1 auto!important;width:auto!important;min-width:74px!important;max-width:none!important;white-space:normal!important;line-height:1.05!important;min-height:29px!important;padding:0 7px!important}
    body #injuriesPanel .segmented button[data-injury-center]{flex:1 1 calc(33.333% - 5px)!important}
    body #injuriesPanel .segmented button[data-injury-status="pending-response"]{flex-basis:calc(50% - 5px)!important}

    body .tabs{overflow:visible!important;flex-wrap:wrap!important}
    body .tabs button{flex:1 1 auto!important;min-width:max-content!important}
  }
</style>


<style id="mobile-card-density-fix-20260525">
  @media(max-width:760px){
    /* Tarjeta móvil densa: cero huecos, información útil arriba */
    body #injuriesPanel tr,
    body #pendingPanel tr,
    body #deletedPanel tr{
      display:grid!important;
      grid-template-columns:minmax(0,1fr) auto!important;
      gap:4px 8px!important;
      padding:9px 10px!important;
      border-radius:13px!important;
      min-height:0!important;
      align-items:start!important;
    }

    body #injuriesPanel td,
    body #pendingPanel td,
    body #deletedPanel td{
      min-height:0!important;
      margin:0!important;
      padding:0!important;
      line-height:1.12!important;
    }

    body #injuriesPanel td::before,
    body #pendingPanel td::before,
    body #deletedPanel td::before{
      display:none!important;
      content:""!important;
    }

    /* Centro pequeño + estado a la derecha */
    body #injuriesPanel td:nth-child(1){grid-column:1!important;grid-row:1!important;font-size:9.5px!important;line-height:1!important;color:rgba(195,191,190,.68)!important;letter-spacing:.08em!important;text-transform:uppercase!important;font-weight:950!important}
    body #injuriesPanel td:nth-child(2){grid-column:2!important;grid-row:1!important;justify-self:end!important;align-self:start!important}
    body #injuriesPanel td:nth-child(2) .muted{display:none!important}
    body #injuriesPanel td:nth-child(2) .risk{min-height:20px!important;line-height:18px!important;font-size:9px!important;padding:0 7px!important}

    /* Nombre + teléfono en bloque principal */
    body #injuriesPanel td:nth-child(3){grid-column:1/-1!important;grid-row:2!important;padding:0!important;margin-top:-1px!important}
    body #injuriesPanel .client-link{display:block!important;font-size:15px!important;line-height:1.05!important;font-weight:950!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;max-width:100%!important}
    body #injuriesPanel .contact{font-size:10.5px!important;line-height:1.05!important;margin-top:2px!important;color:rgba(195,191,190,.68)!important}

    /* Tipo + etiqueta en una línea compacta */
    body #injuriesPanel td:nth-child(4){grid-column:1!important;grid-row:3!important;font-size:10.5px!important;color:rgba(239,233,233,.78)!important}
    body #injuriesPanel td:nth-child(5){grid-column:1/-1!important;grid-row:4!important}
    body #injuriesPanel td:nth-child(5) .name{font-size:12.5px!important;line-height:1.08!important;font-weight:900!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;display:block!important}
    body #injuriesPanel td:nth-child(5) .muted{display:none!important}

    /* Lesión/nota: máximo 2 líneas total para no desperdiciar pantalla */
    body #injuriesPanel td:nth-child(6){grid-column:1/-1!important;grid-row:5!important}
    body #injuriesPanel td:nth-child(6) .reasons{font-size:11.5px!important;line-height:1.18!important;color:rgba(239,233,233,.84)!important;display:-webkit-box!important;-webkit-line-clamp:2!important;-webkit-box-orient:vertical!important;overflow:hidden!important;margin:0!important}
    body #injuriesPanel td:nth-child(7){grid-column:2!important;grid-row:3!important;justify-self:end!important;font-size:10.5px!important;line-height:1.05!important;text-align:right!important;color:rgba(166,201,119,.88)!important;white-space:nowrap!important}
    body #injuriesPanel td:nth-child(8){grid-column:1/-1!important;grid-row:6!important}
    body #injuriesPanel td:nth-child(8) .reasons{display:block!important;font-size:10.7px!important;line-height:1.1!important;color:rgba(195,191,190,.74)!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;margin:0!important}

    /* Acción muy baja, sin convertir media tarjeta en botón */
    body #injuriesPanel td:nth-child(9){grid-column:1/-1!important;grid-row:7!important;margin-top:4px!important;padding-top:6px!important;border-top:1px solid rgba(239,233,233,.07)!important}
    body #injuriesPanel .action-menu-trigger{min-height:32px!important;height:32px!important;border-radius:10px!important;font-size:11px!important;padding:0 10px!important}

    /* Misma filosofía para riesgo si se abre */
    body #membersPanel tr{padding:9px 10px!important;border-radius:13px!important;gap:4px!important}
    body #membersPanel td{line-height:1.14!important;font-size:11.5px!important}
    body #membersPanel td::before{font-size:8px!important;margin-bottom:1px!important}
  }
</style>


<style id="mobile-compact-injury-card-render-20260525">
  body .mobile-injury-summary{display:none}
  @media(max-width:760px){
    body #injuriesPanel tr{display:grid!important;grid-template-columns:1fr!important;gap:7px!important;padding:10px!important;border-radius:13px!important;min-height:0!important}
    body #injuriesPanel td:nth-child(1),
    body #injuriesPanel td:nth-child(2),
    body #injuriesPanel td:nth-child(4),
    body #injuriesPanel td:nth-child(5),
    body #injuriesPanel td:nth-child(6),
    body #injuriesPanel td:nth-child(7),
    body #injuriesPanel td:nth-child(8){display:none!important}
    body #injuriesPanel td:nth-child(3){display:block!important;grid-column:1!important;grid-row:1!important;padding:0!important;margin:0!important;min-height:0!important}
    body #injuriesPanel td:nth-child(9){display:block!important;grid-column:1!important;grid-row:2!important;margin:0!important;padding:0!important;border:0!important}
    body #injuriesPanel td:nth-child(9)::before{display:none!important}
    body #injuriesPanel td::before{display:none!important}
    body #injuriesPanel .client-link{font-size:15px!important;line-height:1.08!important;font-weight:950!important;display:block!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;margin:0 0 5px!important;text-decoration:none!important}
    body #injuriesPanel td:nth-child(3) > .contact{display:none!important}
    body #injuriesPanel .mobile-injury-summary{display:grid!important;gap:5px!important;min-width:0!important}
    body #injuriesPanel .mobile-meta{display:flex!important;align-items:center!important;gap:6px!important;min-width:0!important;overflow:hidden!important;color:rgba(195,191,190,.78)!important;font-size:10.8px!important;line-height:1.05!important;white-space:nowrap!important}
    body #injuriesPanel .mobile-meta span:not(.risk){min-width:0!important;overflow:hidden!important;text-overflow:ellipsis!important;white-space:nowrap!important}
    body #injuriesPanel .mobile-meta .risk{flex:0 0 auto!important;min-height:20px!important;line-height:18px!important;font-size:9px!important;padding:0 7px!important}
    body #injuriesPanel .mobile-desc{font-size:12px!important;line-height:1.18!important;color:rgba(239,233,233,.88)!important;display:-webkit-box!important;-webkit-line-clamp:2!important;-webkit-box-orient:vertical!important;overflow:hidden!important;margin:0!important}
    body #injuriesPanel .mobile-note{font-size:10.8px!important;line-height:1.12!important;color:rgba(195,191,190,.76)!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;margin:0!important}
    body #injuriesPanel .action-menu-trigger{height:30px!important;min-height:30px!important;border-radius:10px!important;font-size:11px!important;box-shadow:none!important;margin-top:1px!important}
  }
</style>


<style id="mobile-compact-injury-card-specificity-fix-20260525">
  @media(max-width:760px){
    body #injuriesPanel tbody tr > td:nth-child(1),
    body #injuriesPanel tbody tr > td:nth-child(2),
    body #injuriesPanel tbody tr > td:nth-child(4),
    body #injuriesPanel tbody tr > td:nth-child(5),
    body #injuriesPanel tbody tr > td:nth-child(6),
    body #injuriesPanel tbody tr > td:nth-child(7),
    body #injuriesPanel tbody tr > td:nth-child(8){display:none!important;width:0!important;height:0!important;max-height:0!important;padding:0!important;margin:0!important;overflow:hidden!important;border:0!important}
    body #injuriesPanel tbody tr > td:nth-child(3){display:block!important;width:100%!important;height:auto!important;max-height:none!important;grid-column:1!important;grid-row:1!important;padding:0!important;margin:0!important;overflow:visible!important;border:0!important}
    body #injuriesPanel tbody tr > td:nth-child(9){display:block!important;width:100%!important;height:auto!important;max-height:none!important;grid-column:1!important;grid-row:2!important;padding:0!important;margin:0!important;overflow:visible!important;border:0!important}
    body #injuriesPanel tbody tr{display:grid!important;grid-template-columns:1fr!important;gap:7px!important;padding:10px!important;min-height:0!important}
  }
</style>


<style id="mobile-compact-injury-card-gap-kill-20260525">
  @media(max-width:760px){
    html body #injuriesPanel tbody tr:not(.expanded) > td:nth-child(1),html body #injuriesPanel tbody tr > td:nth-child(1),
    html body #injuriesPanel tbody tr:not(.expanded) > td:nth-child(2),html body #injuriesPanel tbody tr > td:nth-child(2),
    html body #injuriesPanel tbody tr:not(.expanded) > td:nth-child(4),html body #injuriesPanel tbody tr > td:nth-child(4),
    html body #injuriesPanel tbody tr:not(.expanded) > td:nth-child(5),html body #injuriesPanel tbody tr > td:nth-child(5),
    html body #injuriesPanel tbody tr:not(.expanded) > td:nth-child(6),html body #injuriesPanel tbody tr > td:nth-child(6),
    html body #injuriesPanel tbody tr:not(.expanded) > td:nth-child(7),html body #injuriesPanel tbody tr > td:nth-child(7),
    html body #injuriesPanel tbody tr:not(.expanded) > td:nth-child(8),html body #injuriesPanel tbody tr > td:nth-child(8){display:none!important;position:absolute!important;visibility:hidden!important;inset:auto!important;width:0!important;height:0!important;max-height:0!important;min-height:0!important;padding:0!important;margin:0!important;border:0!important;overflow:hidden!important;grid-row:auto!important;grid-column:auto!important}
    html body #injuriesPanel tbody tr > td:nth-child(3){display:block!important;position:static!important;visibility:visible!important;grid-column:1!important;grid-row:1!important}
    html body #injuriesPanel tbody tr > td:nth-child(9){display:block!important;position:static!important;visibility:visible!important;grid-column:1!important;grid-row:2!important}
    html body #injuriesPanel tbody tr{gap:6px!important;padding:9px 10px!important}
  }
</style>


<style id="mobile-action-button-width-fix-20260525">
  @media(max-width:760px){
    html body #injuriesPanel tbody tr > td:nth-child(9),
    html body #injuriesPanel tbody tr > td:nth-child(9) .action-menu-trigger{width:100%!important;max-width:100%!important;min-width:0!important;box-sizing:border-box!important;justify-self:stretch!important;overflow:hidden!important}
    html body #injuriesPanel tbody tr > td:nth-child(9) .action-menu-trigger{display:flex!important;align-items:center!important;justify-content:center!important}
  }
</style>


<style id="mobile-injury-table-width-reset-20260525">
  @media(max-width:760px){
    html body #injuriesPanel .table-wrap{overflow:visible!important;width:100%!important;max-width:100%!important}
    html body #injuriesPanel table{display:block!important;width:100%!important;min-width:0!important;max-width:100%!important;table-layout:auto!important}
    html body #injuriesPanel thead{display:none!important}
    html body #injuriesPanel tbody{display:grid!important;width:100%!important;max-width:100%!important;min-width:0!important;gap:7px!important}
    html body #injuriesPanel tbody tr{width:100%!important;max-width:100%!important;min-width:0!important;box-sizing:border-box!important}
    html body #injuriesPanel tbody tr > td{max-width:100%!important;box-sizing:border-box!important}
  }
</style>

<style id="inactive-like-injuries-view-20260526">
  body .mobile-inactive-summary{display:none}
  body .inactive-actions{display:flex!important;align-items:center!important;gap:6px!important;justify-content:flex-start!important;flex-wrap:wrap!important}
  body .inactive-workflow-action,body .inactive-profile-trigger{display:flex!important;align-items:center!important;justify-content:center!important;gap:8px!important;min-height:34px!important;padding:0 10px!important;border:1px solid rgba(136,175,96,.38)!important;border-radius:6px!important;background:transparent!important;color:#EAF4E2!important;font-weight:800!important;box-shadow:none!important;cursor:pointer!important}
  body .inactive-workflow-action:hover,body .inactive-profile-trigger:hover{background:rgba(136,175,96,.10)!important;border-color:rgba(166,201,119,.62)!important}
  @media(min-width:761px){
    body #inactivePanel table{min-width:1180px!important;table-layout:fixed!important}
    body #inactivePanel th:nth-child(1),body #inactivePanel td:nth-child(1){width:9%!important}
    body #inactivePanel th:nth-child(2),body #inactivePanel td:nth-child(2){width:10%!important}
    body #inactivePanel th:nth-child(3),body #inactivePanel td:nth-child(3){width:25%!important}
    body #inactivePanel th:nth-child(4),body #inactivePanel td:nth-child(4){width:18%!important}
    body #inactivePanel th:nth-child(5),body #inactivePanel td:nth-child(5){width:9%!important}
    body #inactivePanel th:nth-child(6),body #inactivePanel td:nth-child(6){width:13%!important}
    body #inactivePanel th:nth-child(7),body #inactivePanel td:nth-child(7){width:16%!important;padding-left:5px!important;padding-right:5px!important}
    body #inactivePanel .inactive-profile-trigger{min-width:0!important;width:42px!important;max-width:42px!important;height:30px!important;min-height:30px!important;padding:0!important;margin:0 auto!important;border-radius:6px!important;font-size:0!important;box-shadow:none!important}
    body #inactivePanel .inactive-profile-trigger::before{content:"⋯";font-size:20px!important;line-height:1!important;color:#EAF4E2!important}
  }
  @media(max-width:760px){
    body #inactivePanel .toolbar{display:grid!important;grid-template-columns:1fr!important;gap:7px!important;margin-bottom:8px!important}
    body #inactivePanel .search{height:38px!important;min-height:38px!important;border-radius:12px!important;font-size:13px!important;padding:0 11px!important;background:rgba(9,11,9,.92)!important;border:1px solid rgba(166,201,119,.32)!important}
    body #inactivePanel .segmented{display:flex!important;flex-wrap:wrap!important;overflow:visible!important;gap:5px!important;width:100%!important;padding:3px!important;border-radius:13px!important;background:rgba(239,233,233,.055)!important}
    body #inactivePanel .segmented button{flex:1 1 auto!important;width:auto!important;min-width:74px!important;max-width:none!important;white-space:normal!important;line-height:1.05!important;min-height:29px!important;padding:0 7px!important;font-size:10px!important}
    body #inactiveCountLabel{font-size:11px!important;color:rgba(239,233,233,.66)!important;justify-self:start!important}
    body #inactiveMeta{font-size:11px!important;line-height:1.25!important;margin-bottom:8px!important}
    body #inactivePanel .table-wrap{overflow:visible!important;width:100%!important;max-width:100%!important;background:transparent!important;border:0!important;box-shadow:none!important;border-radius:0!important}
    body #inactivePanel table{display:block!important;width:100%!important;min-width:0!important;max-width:100%!important;table-layout:auto!important;background:transparent!important}
    body #inactivePanel thead{display:none!important}
    body #inactivePanel tbody{display:grid!important;width:100%!important;max-width:100%!important;min-width:0!important;gap:7px!important}
    body #inactivePanel tbody tr{display:grid!important;grid-template-columns:1fr!important;gap:6px!important;width:100%!important;max-width:100%!important;min-width:0!important;box-sizing:border-box!important;padding:9px 10px!important;border:1px solid rgba(239,233,233,.12)!important;border-radius:13px!important;background:linear-gradient(180deg,rgba(31,36,30,.84),rgba(15,18,14,.94))!important;box-shadow:0 10px 24px rgba(0,0,0,.18)!important}
    body #inactivePanel tbody tr > td{max-width:100%!important;box-sizing:border-box!important;padding:0!important;margin:0!important;border:0!important;min-height:0!important;line-height:1.12!important}
    body #inactivePanel tbody tr > td:nth-child(1),
    body #inactivePanel tbody tr > td:nth-child(2),
    body #inactivePanel tbody tr > td:nth-child(4),
    body #inactivePanel tbody tr > td:nth-child(5),
    body #inactivePanel tbody tr > td:nth-child(6){display:none!important;position:absolute!important;visibility:hidden!important;inset:auto!important;width:0!important;height:0!important;max-height:0!important;min-height:0!important;padding:0!important;margin:0!important;border:0!important;overflow:hidden!important;grid-row:auto!important;grid-column:auto!important}
    body #inactivePanel tbody tr > td:nth-child(3){display:block!important;position:static!important;visibility:visible!important;grid-column:1!important;grid-row:1!important;width:100%!important;overflow:visible!important}
    body #inactivePanel tbody tr > td:nth-child(7){display:block!important;position:static!important;visibility:visible!important;grid-column:1!important;grid-row:2!important;width:100%!important;overflow:visible!important}
    body #inactivePanel td::before{display:none!important;content:""!important}
    body #inactivePanel .client-link{font-size:15px!important;line-height:1.08!important;font-weight:950!important;display:block!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;margin:0 0 5px!important;text-decoration:none!important;color:var(--cf-text,#EFE9E9)!important;background:transparent!important;border:0!important;padding:0!important;min-height:0!important}
    body #inactivePanel td:nth-child(3) > .contact{display:none!important}
    body #inactivePanel .mobile-inactive-summary{display:grid!important;gap:5px!important;min-width:0!important}
    body #inactivePanel .mobile-meta{display:flex!important;align-items:center!important;gap:6px!important;min-width:0!important;overflow:hidden!important;color:rgba(195,191,190,.78)!important;font-size:10.8px!important;line-height:1.05!important;white-space:nowrap!important}
    body #inactivePanel .mobile-meta span{min-width:0!important;overflow:hidden!important;text-overflow:ellipsis!important;white-space:nowrap!important}
    body #inactivePanel .mobile-desc{font-size:12px!important;line-height:1.18!important;color:rgba(239,233,233,.88)!important;display:-webkit-box!important;-webkit-line-clamp:2!important;-webkit-box-orient:vertical!important;overflow:hidden!important;margin:0!important}
    body #inactivePanel .mobile-note{font-size:10.8px!important;line-height:1.12!important;color:rgba(195,191,190,.76)!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;margin:0!important}
    body #inactivePanel .inactive-actions{display:grid!important;grid-template-columns:1fr!important;gap:6px!important;width:100%!important}
    body #inactivePanel .inactive-workflow-action,body #inactivePanel .inactive-profile-trigger{width:100%!important;max-width:100%!important;min-width:0!important;height:30px!important;min-height:30px!important;border-radius:6px!important;font-size:11px!important;box-shadow:none!important;margin-top:1px!important;box-sizing:border-box!important}
  }
</style>

<style id="injuries-plain-data-no-pills-final-20260526">
  html body #injuriesPanel .risk,
  html body #injuriesPanel .mobile-meta .risk,
  html body #injuriesPanel td:nth-child(2) .risk{
    display:inline!important;
    width:auto!important;
    max-width:100%!important;
    min-height:0!important;
    height:auto!important;
    padding:0!important;
    margin:0!important;
    border:0!important;
    border-radius:0!important;
    background:transparent!important;
    box-shadow:none!important;
    line-height:inherit!important;
    font-size:inherit!important;
    white-space:normal!important;
  }
  html body #injuriesPanel .risk.Bajo,
  html body #injuriesPanel .risk.Al.dia{color:var(--cf-green2,#A6C977)!important}
  html body #injuriesPanel .risk.Medio,
  html body #injuriesPanel .risk.Hoy{color:#FFD18A!important}
  html body #injuriesPanel .risk.Alto,
  html body #injuriesPanel .risk.Vencido{color:#FFB1A8!important}
  html body #injuriesPanel .risk.Proximos{color:#ABC8FF!important}
  html body #injuriesPanel .risk.Sin.fecha{color:rgba(195,191,190,.86)!important}
  @media(max-width:760px){
    html body #injuriesPanel .mobile-meta .risk{flex:0 0 auto!important;font-weight:950!important;font-size:10.8px!important;line-height:1.05!important}
  }
</style>

<style id="inactive-plain-name-no-pill-final-20260529">
  html body #inactivePanel .client-link,
  html body #inactivePanel button.client-link{
    appearance:none!important;
    -webkit-appearance:none!important;
    display:inline!important;
    width:auto!important;
    max-width:100%!important;
    min-height:0!important;
    height:auto!important;
    padding:0!important;
    margin:0!important;
    border:0!important;
    border-radius:0!important;
    background:transparent!important;
    box-shadow:none!important;
    color:var(--cf-text,#EFE9E9)!important;
    font:inherit!important;
    font-weight:950!important;
    line-height:inherit!important;
    text-align:left!important;
    text-decoration:none!important;
    cursor:pointer!important;
  }
  html body #inactivePanel .client-link:hover,
  html body #inactivePanel button.client-link:hover{
    background:transparent!important;
    border:0!important;
    box-shadow:none!important;
    color:var(--cf-green2,#A6C977)!important;
    text-decoration:none!important;
  }
  @media(max-width:760px){
    html body #inactivePanel .client-link,
    html body #inactivePanel button.client-link{
      display:block!important;
      white-space:nowrap!important;
      overflow:hidden!important;
      text-overflow:ellipsis!important;
      font-size:15px!important;
      line-height:1.08!important;
      margin:0 0 5px!important;
    }
  }
</style>

<style id="injuries-plain-name-no-pill-final-20260526">
  html body #injuriesPanel .client-link,
  html body #injuriesPanel button.client-link{
    appearance:none!important;
    -webkit-appearance:none!important;
    display:inline!important;
    width:auto!important;
    max-width:100%!important;
    min-height:0!important;
    height:auto!important;
    padding:0!important;
    margin:0!important;
    border:0!important;
    border-radius:0!important;
    background:transparent!important;
    box-shadow:none!important;
    color:var(--cf-text,#EFE9E9)!important;
    font:inherit!important;
    font-weight:950!important;
    line-height:inherit!important;
    text-align:left!important;
    text-decoration:none!important;
    cursor:pointer!important;
  }
  html body #injuriesPanel .client-link:hover,
  html body #injuriesPanel button.client-link:hover{
    background:transparent!important;
    border:0!important;
    box-shadow:none!important;
    color:var(--cf-green2,#A6C977)!important;
    text-decoration:none!important;
  }
  @media(max-width:760px){
    html body #injuriesPanel .client-link,
    html body #injuriesPanel button.client-link{
      display:block!important;
      white-space:nowrap!important;
      overflow:hidden!important;
      text-overflow:ellipsis!important;
      font-size:15px!important;
      line-height:1.08!important;
      margin:0 0 5px!important;
    }
  }
</style>

<style id="injuries-last-action-wide-fix-20260529">
  @media(min-width:761px){
    html body #injuriesPanel table{width:100%!important;min-width:0!important;table-layout:fixed!important}
    html body #injuriesPanel .table-wrap{overflow-x:hidden!important}
    html body #injuriesPanel th,html body #injuriesPanel td{box-sizing:border-box!important}
    html body #injuriesPanel th:nth-child(1),html body #injuriesPanel td:nth-child(1){width:6%!important;min-width:0!important;max-width:none!important}
    html body #injuriesPanel th:nth-child(2),html body #injuriesPanel td:nth-child(2){width:10%!important;min-width:0!important;max-width:none!important}
    html body #injuriesPanel th:nth-child(3),html body #injuriesPanel td:nth-child(3){width:18%!important;min-width:0!important;max-width:none!important}
    html body #injuriesPanel th:nth-child(4),html body #injuriesPanel td:nth-child(4){width:9%!important;min-width:0!important;max-width:none!important}
    html body #injuriesPanel th:nth-child(5),html body #injuriesPanel td:nth-child(5){width:4%!important;min-width:0!important;max-width:none!important}
    html body #injuriesPanel th:nth-child(6),html body #injuriesPanel td:nth-child(6){width:5%!important;min-width:0!important;max-width:none!important}
    html body #injuriesPanel th:nth-child(7),html body #injuriesPanel td:nth-child(7){width:20%!important;min-width:0!important;max-width:none!important}
    html body #injuriesPanel th:nth-child(8),html body #injuriesPanel td:nth-child(8){width:8%!important;min-width:0!important;max-width:none!important}
    html body #injuriesPanel th:nth-child(9),html body #injuriesPanel td:nth-child(9){width:20%!important;min-width:0!important;max-width:none!important;text-align:left!important;padding-left:8px!important;padding-right:8px!important}
    html body #injuriesPanel th:nth-child(9){font-size:inherit!important;white-space:normal!important}
    html body #injuriesPanel th:nth-child(9)::after{display:none!important;content:""!important}
    html body #injuriesPanel td:nth-child(9).reasons{display:block!important;white-space:normal!important;overflow:visible!important;text-overflow:clip!important;max-height:none!important;line-height:1.25!important}
  }
</style>

<style id="injuries-auto-layout-final-20260529">
  /* Desktop/tablet: let the browser size columns from real content instead of forced percentages. */
  @media(min-width:761px){
    html body #injuriesPanel .table-wrap{
      width:100%!important;              /* Fill all available panel width. */
      max-width:100%!important;
      overflow-x:auto!important;         /* Only appears if readable minimums exceed viewport. */
      overflow-y:auto!important;         /* Keep vertical scrolling inside the table area. */
      max-height:calc(100vh - 18rem)!important;
    }
    html body #injuriesPanel table{
      width:100%!important;              /* Table expands to the full container. */
      min-width:fit-content!important;   /* But can grow if content genuinely needs more room. */
      table-layout:auto!important;       /* Content-driven column sizing. */
      border-collapse:separate!important;
      border-spacing:0!important;
      font-size:clamp(.68rem,.72vw,.78rem)!important;
    }
    html body #injuriesPanel thead th{
      position:sticky!important;         /* Header remains visible during contained vertical scroll. */
      top:0!important;
      z-index:5!important;
      background:rgba(18,18,18,.98)!important;
    }
    html body #injuriesPanel th,
    html body #injuriesPanel td{
      width:auto!important;              /* Reset old hardcoded width percentages. */
      min-width:0!important;
      max-width:none!important;
      box-sizing:border-box!important;
      padding:.48rem .55rem!important;
      vertical-align:top!important;
      white-space:normal!important;      /* Allow rows to grow vertically rather than cutting text. */
      overflow:visible!important;
      text-overflow:clip!important;
      overflow-wrap:anywhere!important;
      word-break:normal!important;
      line-height:1.22!important;
    }
    html body #injuriesPanel th button{
      width:100%!important;
      white-space:normal!important;      /* Headers can wrap instead of being truncated. */
      overflow:visible!important;
      text-overflow:clip!important;
      line-height:1.12!important;
    }
    html body #injuriesPanel th:nth-child(1),html body #injuriesPanel td:nth-child(1){min-width:5ch!important}   /* Centro */
    html body #injuriesPanel th:nth-child(2),html body #injuriesPanel td:nth-child(2){min-width:10ch!important}  /* Seguimiento */
    html body #injuriesPanel th:nth-child(3),html body #injuriesPanel td:nth-child(3){min-width:18ch!important}  /* Socio */
    html body #injuriesPanel th:nth-child(4),html body #injuriesPanel td:nth-child(4){min-width:14ch!important}  /* Tarifa */
    html body #injuriesPanel th:nth-child(5),html body #injuriesPanel td:nth-child(5){min-width:4ch!important}   /* Tipo */
    html body #injuriesPanel th:nth-child(6),html body #injuriesPanel td:nth-child(6){min-width:8ch!important}   /* Etiqueta */
    html body #injuriesPanel th:nth-child(7),html body #injuriesPanel td:nth-child(7){min-width:24ch!important}  /* Lesión */
    html body #injuriesPanel th:nth-child(8),html body #injuriesPanel td:nth-child(8){min-width:10ch!important}  /* Próximo contacto */
    html body #injuriesPanel th:nth-child(9),html body #injuriesPanel td:nth-child(9){min-width:24ch!important}  /* Última acción */
    html body #injuriesPanel th:nth-child(10),html body #injuriesPanel td:nth-child(10){min-width:4.5rem!important;width:4.5rem!important;max-width:4.5rem!important;text-align:center!important} /* Acción compacta */
    html body #injuriesPanel th:nth-child(10){font-size:0!important}
    html body #injuriesPanel th:nth-child(10)::after{content:"⋯"!important;font-size:1rem!important;color:var(--cf-muted,#C3BFBE)!important}
    html body #injuriesPanel td:nth-child(10) .action-menu-trigger{min-width:0!important;width:2.5rem!important;max-width:2.5rem!important;height:2rem!important;min-height:2rem!important;padding:0!important;margin:0 auto!important;border-radius:.5rem!important;font-size:0!important;box-shadow:none!important}
    html body #injuriesPanel td:nth-child(10) .action-menu-trigger::before{content:"⋯"!important;font-size:1.2rem!important;line-height:1!important;color:#EAF4E2!important}
    html body #injuriesPanel td .name,
    html body #injuriesPanel td .muted,
    html body #injuriesPanel td .contact,
    html body #injuriesPanel td.reasons,
    html body #injuriesPanel td:nth-child(7) .name,
    html body #injuriesPanel td:nth-child(9).reasons{
      display:block!important;           /* Remove old line-clamp/pill constraints. */
      max-width:100%!important;
      max-height:none!important;
      white-space:normal!important;
      overflow:visible!important;
      text-overflow:clip!important;
      -webkit-line-clamp:unset!important;
      -webkit-box-orient:unset!important;
      overflow-wrap:anywhere!important;
    }
    html body #injuriesPanel td:nth-child(6) .risk{display:none!important} /* Etiqueta: show actual label text, not a yes/no pill. */
    html body #injuriesPanel td:nth-child(6) .muted{display:block!important}
  }
</style>

<style id="inactive-auto-layout-final-20260529">
  /* Desktop/tablet: content-driven table for +7 días sin venir, without changing data or workflow logic. */
  @media(min-width:761px){
    html body #inactivePanel .table-wrap{
      width:100%!important;              /* Always consume the full available panel width. */
      max-width:100%!important;
      overflow-x:auto!important;         /* Horizontal scroll only when readable minimums truly exceed the viewport. */
      overflow-y:auto!important;         /* Keep long inactive lists scrolling inside the table, not the whole page. */
      max-height:calc(100vh - 18rem)!important;
    }
    html body #inactivePanel table{
      width:100%!important;              /* Expand to the parent width even with few columns/short content. */
      min-width:fit-content!important;   /* Allow natural growth only when content minimums need it. */
      table-layout:auto!important;       /* Let columns size from real content instead of fixed percentages. */
      border-collapse:separate!important;
      border-spacing:0!important;
      font-size:clamp(.68rem,.72vw,.78rem)!important;
    }
    html body #inactivePanel thead th{
      position:sticky!important;         /* Headers stay visible while scrolling the contained table. */
      top:0!important;
      z-index:5!important;
      background:rgba(18,18,18,.98)!important;
    }
    html body #inactivePanel th,
    html body #inactivePanel td{
      width:auto!important;              /* Reset older fixed percentage widths. */
      min-width:0!important;
      max-width:none!important;
      box-sizing:border-box!important;
      padding:.48rem .55rem!important;
      vertical-align:top!important;
      white-space:normal!important;      /* Let rows grow vertically instead of clipping variable text. */
      overflow:visible!important;
      text-overflow:clip!important;
      overflow-wrap:anywhere!important;
      word-break:normal!important;
      line-height:1.22!important;
    }
    html body #inactivePanel th button{
      width:100%!important;
      white-space:normal!important;      /* Long headers wrap naturally. */
      overflow:visible!important;
      text-overflow:clip!important;
      line-height:1.12!important;
    }
    html body #inactivePanel th:nth-child(1),html body #inactivePanel td:nth-child(1){min-width:7ch!important}    /* Centro */
    html body #inactivePanel th:nth-child(2),html body #inactivePanel td:nth-child(2){min-width:9ch!important}    /* Estado/días */
    html body #inactivePanel th:nth-child(3),html body #inactivePanel td:nth-child(3){min-width:24ch!important}   /* Socio + contacto */
    html body #inactivePanel th:nth-child(4),html body #inactivePanel td:nth-child(4){min-width:18ch!important}   /* Tarifa */
    html body #inactivePanel th:nth-child(5),html body #inactivePanel td:nth-child(5){min-width:7ch!important}    /* Activa */
    html body #inactivePanel th:nth-child(6),html body #inactivePanel td:nth-child(6){min-width:11ch!important}   /* Última clase */
    html body #inactivePanel th:nth-child(7),html body #inactivePanel td:nth-child(7){min-width:8rem!important}   /* Workflow action only; profile remains accessible from the member name. */
    html body #inactivePanel .client-link,
    html body #inactivePanel .name,
    html body #inactivePanel .contact,
    html body #inactivePanel .muted{
      max-width:100%!important;          /* Remove old ellipsis/clamp behavior on desktop. */
      white-space:normal!important;
      overflow:visible!important;
      text-overflow:clip!important;
      overflow-wrap:anywhere!important;
    }
    html body #inactivePanel .inactive-actions{
      display:flex!important;            /* Keep workflow buttons usable while still wrapping if needed. */
      align-items:center!important;
      justify-content:flex-start!important;
      gap:.4rem!important;
      flex-wrap:wrap!important;
    }
    html body #inactivePanel .inactive-workflow-action,
    html body #inactivePanel .inactive-profile-trigger{
      width:auto!important;              /* Undo previous compact fixed action button. */
      min-width:2.6rem!important;
      max-width:none!important;
      height:auto!important;
      min-height:2rem!important;
      padding:0 .65rem!important;
      font-size:.72rem!important;
    }
  }
</style>
  <div class="shell">
    <aside>
      <div class="brand">
        <strong>Proyecto Risk</strong>
        <span>CrossFit MPO · Las Rosas</span>
      </div>
      <nav class="nav">
        <button class="active" type="button">Panel diario</button>
        <button type="button" data-filter="Alto">Riesgo alto</button>
        <button type="button" data-filter="Medio">Riesgo medio</button>
        <button type="button" data-filter="Bajo">Riesgo bajo</button>
      </nav>
    </aside>
    <main class="main">
      <div class="topbar">
        <div>
          <h1>Riesgo de bajas · Lesionados</h1>
          <p class="subtitle" id="subtitle">Socios con tarifa activa clasificados por riesgo a dia de hoy.</p>
        </div>
        <div class="actions">
          <button class="button primary" id="refreshBtn" type="button" title="Actualiza el listado con AimHarder">↻ Recalcular</button>
        </div>
      </div>

      <section class="kpis" aria-label="Resumen">
        <div class="kpi"><span>Listado</span><strong id="kpiListed">0</strong><small>socios activos</small></div>
        <div class="kpi"><span>Alto</span><strong id="kpiHigh">0</strong><small>requieren contacto</small></div>
        <div class="kpi"><span>Medio</span><strong id="kpiMedium">0</strong><small>vigilar esta semana</small></div>
        <div class="kpi"><span>Bajo</span><strong id="kpiLow">0</strong><small>sin alerta inmediata</small></div>
        <div class="kpi"><span>+30 dias</span><strong id="kpiNoClass">0</strong><small>sin reservar clase</small></div>
        <div class="kpi"><span>Lesionados</span><strong id="kpiInjured">0</strong><small>en hojas beta</small></div>
        <div class="kpi"><span>Seguimiento</span><strong id="kpiInjuryDue">0</strong><small>vencido o cercano</small></div>
        <div class="kpi"><span>+7 días</span><strong id="kpiInactive">0</strong><small>sin venir a entrenar</small></div>
        <div class="kpi"><span>Score medio</span><strong id="kpiAvg">0</strong><small>del listado actual</small></div>
      </section>

      <section class="workspace-nav" aria-label="Areas del panel">
        <div class="nav-group injury-group">
          <div class="nav-group-label">Lesionados y seguimientos</div>
          <div class="tabs" role="tablist" aria-label="Lesionados y seguimientos">
            <button class="active" type="button" data-tab="injuries">Listado lesionados</button>
            <button type="button" data-tab="deleted">Curados</button>
          </div>
        </div>
        <div class="nav-group inactivity-group">
          <div class="nav-group-label">Inactividad</div>
          <div class="tabs" role="tablist" aria-label="Inactividad">
            <button type="button" data-tab="inactive">7+ días sin venir</button>
          </div>
        </div>
        <div class="nav-group tariffs-group">
          <div class="nav-group-label">Tarifas</div>
          <div class="tabs" role="tablist" aria-label="Tarifas">
            <button type="button" data-tab="tariffs">Tarifas completadas</button>
          </div>
        </div>
        <div class="nav-group risk-group">
          <div class="nav-group-label">Riesgo de bajas</div>
          <div class="tabs" role="tablist" aria-label="Riesgo de bajas">
            <button type="button" data-tab="members">Socios en riesgo</button>
            <button type="button" data-tab="rules">Normas score</button>
            <button type="button" data-tab="settings">Ajustes score</button>
          </div>
        </div>
      </section>

      <section class="tab-panel" id="membersPanel" hidden>
        <header class="panel-head risk-head">
          <div><span>Riesgo de bajas</span><h2>Socios que requieren atención</h2></div>
          <p>Listado independiente del módulo de lesionados. Usa búsqueda, filtro por riesgo y ordena cualquier columna.</p>
        </header>
        <section class="toolbar">
          <input class="search" id="search" type="search" placeholder="Buscar por nombre, telefono, email o motivo">
          <div class="segmented" aria-label="Filtro de riesgo">
            <button type="button" class="active" data-risk="Todos">Todos</button>
            <button type="button" data-risk="Alto">Alto</button>
            <button type="button" data-risk="Medio">Medio</button>
            <button type="button" data-risk="Bajo">Bajo</button>
          </div>
          <button class="button" type="button" data-clear="risk">Limpiar filtros</button>
          <div class="muted" id="countLabel">0 visibles</div>
        </section>

        <section class="table-wrap">
          <table>
            <thead>
              <tr>
                <th><button type="button" data-sort="score">Prioridad</button></th>
                <th><button type="button" data-sort="nombre">Socio</button></th>
                <th><button type="button" data-sort="email">Contacto</button></th>
                <th><button type="button" data-sort="dias_sin_clase">Dias sin clase</button></th>
                <th><button type="button" data-sort="ultimo_pago_tarifa">Ultimo pago tarifa</button></th>
                <th><button type="button" data-sort="tarifa">Tarifa</button></th>
                <th><button type="button" data-sort="motivos">Motivos</button></th>
              </tr>
            </thead>
            <tbody id="rows"></tbody>
          </table>
          <div class="empty" id="empty" hidden>No hay resultados con esos filtros.</div>
        </section>
      </section>

      <section class="tab-panel" id="pendingPanel" hidden>
        <header class="panel-head injury-head">
          <div><span>Lesionados</span><h2>Seguimientos pendientes</h2></div>
          <p>Vencidos, sin fecha, pendientes de respuesta o con próximo contacto en menos de 7 días. Desde aquí puedes marcar como hecho o cerrar el seguimiento.</p>
        </header>
        <section class="toolbar injury-toolbar">
          <input class="search" id="pendingSearch" type="search" placeholder="Buscar pendiente, telefono, lesión o nota">
          <div class="segmented" aria-label="Filtro por centro pendientes">
            <button type="button" class="active" data-pending-center="Getafe">Getafe</button>
            <button type="button" data-pending-center="Parla">Parla</button>
            <button type="button" data-pending-center="Las Rosas">Las Rosas</button>
          </div>
          <button class="button" type="button" data-clear="pending">Limpiar filtros</button>
          <div class="muted" id="pendingCountLabel">0 pendientes</div>
        </section>
        <section class="table-wrap">
          <table>
            <thead>
              <tr>
                <th><button type="button" data-pending-sort="days_remaining">Vence</button></th>
                <th><button type="button" data-pending-sort="center">Centro</button></th>
                <th><button type="button" data-pending-sort="name">Socio</button></th>
                <th><button type="button" data-pending-sort="label">Lesion</button></th>
                <th><button type="button" data-pending-sort="next_contact">Proximo contacto</button></th>
                <th><button type="button" data-pending-sort="latest_note">Última acción</button></th>
                <th>Accion</th>
              </tr>
            </thead>
            <tbody id="pendingRows"></tbody>
          </table>
          <div class="empty" id="pendingEmpty" hidden>No hay seguimientos pendientes en menos de 7 dias.</div>
        </section>
      </section>

      <section class="tab-panel" id="injuriesPanel">
        <header class="panel-head injury-head">
          <div><span>Lesionados</span><h2>Listado completo</h2></div>
          <p>Base activa de lesionados por centro y estado de seguimiento. No se mezcla con el score de riesgo.</p>
        </header>
        <section class="toolbar injury-toolbar">
          <input class="search" id="injurySearch" type="search" placeholder="Buscar lesionado, telefono, etiqueta o nota">
          <div class="segmented" aria-label="Filtro por centro">
            <button type="button" class="active" data-injury-center="Getafe">Getafe</button>
            <button type="button" data-injury-center="Parla">Parla</button>
            <button type="button" data-injury-center="Las Rosas">Las Rosas</button>
          </div>
          <div class="segmented" aria-label="Filtro de seguimiento">
            <button type="button" class="active" data-injury-status="Todos">Todos</button>
            <button type="button" data-injury-status="Nuevos">Nuevos</button>
            <button type="button" data-injury-status="Pendientes de escribir">Pendientes de escribir</button>
            <button type="button" data-injury-status="Pendiente respuesta">Pendiente respuesta</button>
            <button type="button" data-injury-status="Al dia">Al dia</button>
            <button type="button" data-injury-status="Sin fecha">Sin fecha</button>
            <button type="button" data-injury-status="Sin seguimiento">Sin seguimiento</button>
          </div>
          <button class="button" type="button" data-clear="injuries">Limpiar filtros</button>
          <div class="muted" id="injuryCountLabel">0 visibles</div>
        </section>
        <section class="table-wrap">
          <table>
            <thead>
              <tr>
                <th><button type="button" data-injury-sort="center">Centro</button></th>
                <th><button type="button" data-injury-sort="status">Seguimiento</button></th>
                <th><button type="button" data-injury-sort="name">Socio</button></th>
                <th><button type="button" data-injury-sort="membership_name">Tarifa</button></th>
                <th><button type="button" data-injury-sort="type">Tipo</button></th>
                <th><button type="button" data-injury-sort="label">Etiqueta</button></th>
                <th>Lesion</th>
                <th><button type="button" data-injury-sort="next_contact">Proximo contacto</button></th>
                <th><button type="button" data-injury-sort="latest_note">Última acción</button></th>
                <th>Acción</th>
              </tr>
            </thead>
            <tbody id="injuryRows"></tbody>
          </table>
          <div class="empty" id="injuryEmpty" hidden>No hay lesionados con esos filtros.</div>
        </section>
      </section>

      <section class="tab-panel" id="inactivePanel" hidden>
        <header class="panel-head risk-head">
          <div><span>Inactividad</span><h2>Socios 7+ días sin venir</h2></div>
          <p>Flujo único por centro, ordenado de menos a más tiempo sin venir.</p>
        </header>
        <section class="toolbar">
          <input class="search" id="inactiveSearch" type="search" placeholder="Buscar por nombre, teléfono, email o tarifa">
          <div class="segmented" aria-label="Filtro por centro inactivos">
            <button type="button" class="active" data-inactive-center="Getafe">Getafe</button>
            <button type="button" data-inactive-center="Parla">Parla</button>
            <button type="button" data-inactive-center="Las Rosas">Las Rosas</button>
          </div>
          <div class="segmented" aria-label="Estado del flujo de inactivos">
            <button type="button" data-inactive-workflow="all">Todos</button>
            <button type="button" class="active" data-inactive-workflow="pending">Pendientes de escribir</button>
            <button type="button" data-inactive-workflow="review">En revisión</button>
            <button type="button" data-inactive-workflow="done">Hecho</button>
          </div>
          <button class="button primary" id="inactiveRefreshBtn" type="button">↻ Actualizar inactivos</button>
          <button class="button" type="button" data-clear="inactive">Limpiar filtros</button>
          <div class="muted" id="inactiveCountLabel">0 visibles</div>
        </section>
        <div class="warn" id="inactiveMeta">Pendiente de actualizar.</div>
        <section class="table-wrap">
          <table>
            <thead>
              <tr>
                <th><button type="button" data-inactive-sort="center">Centro</button></th>
                <th><button type="button" data-inactive-sort="days_without_class">Estado</button></th>
                <th><button type="button" data-inactive-sort="name">Socio</button></th>
                <th><button type="button" data-inactive-sort="membership_name">Tarifa</button></th>
                <th><button type="button" data-inactive-sort="membership_active">Activa</button></th>
                <th><button type="button" data-inactive-sort="last_class_at">Última clase</button></th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody id="inactiveRows"></tbody>
          </table>
          <div class="empty" id="inactiveEmpty" hidden>No hay socios con esos filtros.</div>
        </section>
      </section>

      <section class="tab-panel" id="tariffsPanel" hidden>
        <header class="panel-head risk-head">
          <div><span>Tarifas</span><h2>Tarifas completadas · Parla</h2></div>
          <p>Listado inicial de socios de Parla con tarifas limitadas detectadas. Por ahora no envía avisos: solo muestra el estado del cálculo.</p>
        </header>
        <section class="toolbar">
          <input class="search" id="tariffSearch" type="search" placeholder="Buscar por socio, teléfono, email o tarifa">
          <button class="button primary" id="tariffRefreshBtn" type="button">↻ Actualizar tarifas</button>
          <button class="button" type="button" data-clear="tariffs">Limpiar búsqueda</button>
          <div class="muted" id="tariffCountLabel">0 visibles</div>
        </section>
        <div class="warn" id="tariffMeta">Pendiente de actualizar.</div>
        <section class="table-wrap">
          <table>
            <thead>
              <tr>
                <th><button type="button" data-tariff-sort="name">Socio</button></th>
                <th><button type="button" data-tariff-sort="membership_name">Tarifa</button></th>
                <th><button type="button" data-tariff-sort="cycle_start">Inicio ciclo</button></th>
                <th><button type="button" data-tariff-sort="contracted_classes">Contratadas</button></th>
                <th><button type="button" data-tariff-sort="consumed_classes">Consumidas</button></th>
                <th><button type="button" data-tariff-sort="remaining_classes">Restantes</button></th>
                <th><button type="button" data-tariff-sort="last_class_at">Última clase</button></th>
                <th><button type="button" data-tariff-sort="status">Estado</button></th>
              </tr>
            </thead>
            <tbody id="tariffRows"></tbody>
          </table>
          <div class="empty" id="tariffEmpty" hidden>No hay tarifas con esos filtros.</div>
        </section>
      </section>

      <section class="tab-panel" id="deletedPanel" hidden>
        <header class="panel-head injury-head">
          <div><span>Lesionados</span><h2>Registro de curados</h2></div>
          <p>Historial de seguimientos cerrados. Se conserva la información y la nota automática de baja.</p>
        </header>
        <section class="toolbar">
          <input class="search" id="deletedSearch" type="search" placeholder="Buscar curado, teléfono, lesión o historial">
          <button class="button" type="button" data-clear="deleted">Limpiar búsqueda</button>
          <div class="muted" id="deletedCountLabel">0 curados</div>
        </section>
        <section class="table-wrap">
          <table>
            <thead>
              <tr>
                <th><button type="button" data-deleted-sort="updated_at">Curado</button></th>
                <th><button type="button" data-deleted-sort="center">Centro</button></th>
                <th><button type="button" data-deleted-sort="name">Socio</button></th>
                <th><button type="button" data-deleted-sort="label">Lesion</button></th>
                <th><button type="button" data-deleted-sort="latest_note">Historial / nota</button></th>
              </tr>
            </thead>
            <tbody id="deletedRows"></tbody>
          </table>
          <div class="empty" id="deletedEmpty" hidden>No hay registros curados.</div>
        </section>
      </section>

      <section class="tab-panel rules" id="rulesPanel" aria-label="Normas de puntuacion" hidden>
        <div class="rules-panel">
          <h2>Niveles de riesgo</h2>
          <div class="legend">
            <div class="legend-row"><span class="risk Alto">Alto</span><span>61 puntos o mas</span></div>
            <div class="legend-row"><span class="risk Medio">Medio</span><span>31 a 60 puntos</span></div>
            <div class="legend-row"><span class="risk Bajo">Bajo</span><span>0 a 30 puntos</span></div>
          </div>
        </div>
        <div class="rules-panel">
          <h2>Como se calcula</h2>
          <div class="rule-grid">
            <div class="rule-block"><strong>Dias sin clase</strong><p>+40 si supera 30 dias. +32 si supera 21. +22 si supera 14. +10 si supera 7. +28 si no hay reserva registrada.</p></div>
            <div class="rule-block"><strong>Antiguedad</strong><p>+18 durante el primer mes. +15 en los primeros 3 meses. +8 en los primeros 6 meses.</p></div>
            <div class="rule-block"><strong>Tarifa</strong><p>Academy 2D +20. Academy +15. S o 9 clases +12. M o 13 clases +6. L y XL +0.</p></div>
            <div class="rule-block"><strong>Uso semanal</strong><p>Si existe media semanal: +18 por menos de 0,5 reservas/semana. +10 por menos de 1 reserva/semana.</p></div>
            <div class="rule-block"><strong>Pagos fallidos</strong><p>+10 por pago fallido reciente, con un maximo de +25 puntos.</p></div>
            <div class="rule-block"><strong>Filtro previo</strong><p>Solo entran socios con tarifa activa detectada en el CSV de pagos del mes y cruzados con AimHarder. Wellhub/Gympass queda excluido de este seguimiento.</p></div>
          </div>
        </div>
      </section>

      <section class="tab-panel settings" id="settingsPanel" aria-label="Ajustes de puntuacion" hidden>
        <div class="settings-panel">
          <h2>Perfiles de calculo</h2>
          <div class="profile-grid">
            <button class="profile-card" type="button" data-profile="permisivo"><strong>Permisivo</strong><span>Menos sensible. Recomendado si quieres ver solo señales muy claras.</span></button>
            <button class="profile-card active" type="button" data-profile="equilibrado"><strong>Equilibrado</strong><span>Punto de partida actual para revisar riesgos accionables.</span></button>
            <button class="profile-card" type="button" data-profile="severo"><strong>Severo</strong><span>Mas sensible. Sube antes a medio o alto.</span></button>
          </div>
        </div>
        <div class="settings-panel">
          <h2>Puntos ajustables</h2>
          <div class="control-grid" id="configControls"></div>
        </div>
      </section>
    </main>
  </div>
  <div class="profile-modal" id="clientProfileModal" hidden>
    <div class="profile-card-modal">
      <div class="profile-head">
        <div>
          <h2 id="profileName">Perfil cliente</h2>
          <p class="subtitle" id="profileSubtitle"></p>
        </div>
        <button class="button profile-close" id="profileClose" type="button">Cerrar</button>
      </div>
      <div class="profile-grid">
        <div class="profile-mini"><span>Teléfono</span><strong id="profilePhone">—</strong></div>
        <div class="profile-mini"><span>Email</span><strong id="profileEmail">—</strong></div>
        <div class="profile-mini"><span>Centro</span><strong id="profileCenter">—</strong></div>
        <div class="profile-mini"><span>ID externo</span><strong id="profileExternal">—</strong></div>
        <div class="profile-mini"><span>Tarifa</span><strong id="profileTariff">—</strong></div>
      </div>
      <details class="profile-injury-edit" id="profileInjuryEdit" hidden>
        <summary>Editar lesión</summary>
        <div class="profile-injury-edit-body">
          <div class="profile-injury-grid">
            <label>Tipo de lesión
              <select id="profileInjuryType">
                <option value="1">Tipo 1</option>
                <option value="2">Tipo 2</option>
                <option value="3">Tipo 3</option>
              </select>
            </label>
            <label>Motivo / detalle de la lesión
              <textarea id="profileInjuryDescription" placeholder="Motivo de la lesión, zona afectada o detalle operativo para coaches..."></textarea>
            </label>
          </div>
          <button class="button primary" id="profileSaveInjury" type="button">Guardar cambios de lesión</button>
        </div>
      </details>
      <div class="profile-note-box">
        <label class="muted" for="profileNote">Añadir nota</label>
        <textarea id="profileNote" placeholder="Ej: llamada realizada, adaptación, detalle importante..."></textarea>
        <button class="button primary" id="profileSaveNote" type="button">Guardar nota</button>
      </div>
      <h3>Notas</h3>
      <div class="profile-list" id="profileNotes"></div>
      <h3>Historial</h3>
      <div class="profile-list" id="profileEvents"></div>
    </div>
  </div>

  <script>
    const profiles = {
      permisivo: {
        highThreshold: 70, mediumThreshold: 40,
        days30: 32, days21: 24, days14: 16, days7: 6, noBooking: 20,
        age1: 12, age3: 10, age6: 5,
        academy2d: 14, academy: 10, tariffS: 8, tariffM: 4, otherTariff: 2,
        weeklyVeryLow: 12, weeklyLow: 6, failedPayment: 8, failedPaymentMax: 20,
      },
      equilibrado: {
        highThreshold: 61, mediumThreshold: 31,
        days30: 40, days21: 32, days14: 22, days7: 10, noBooking: 28,
        age1: 18, age3: 15, age6: 8,
        academy2d: 20, academy: 15, tariffS: 12, tariffM: 6, otherTariff: 3,
        weeklyVeryLow: 18, weeklyLow: 10, failedPayment: 10, failedPaymentMax: 25,
      },
      severo: {
        highThreshold: 55, mediumThreshold: 25,
        days30: 48, days21: 40, days14: 30, days7: 16, noBooking: 35,
        age1: 24, age3: 20, age6: 12,
        academy2d: 26, academy: 20, tariffS: 16, tariffM: 9, otherTariff: 5,
        weeklyVeryLow: 24, weeklyLow: 14, failedPayment: 12, failedPaymentMax: 30,
      },
    };
    const controlDefs = [
      ['highThreshold', 'Umbral Alto', 40, 90],
      ['mediumThreshold', 'Umbral Medio', 10, 70],
      ['days30', '>30 dias sin clase', 0, 70],
      ['days21', '>21 dias sin clase', 0, 60],
      ['days14', '>14 dias sin clase', 0, 50],
      ['days7', '>7 dias sin clase', 0, 35],
      ['noBooking', 'Sin reserva registrada', 0, 60],
      ['age1', 'Alta: primer mes', 0, 40],
      ['age3', 'Alta: primeros 3 meses', 0, 35],
      ['age6', 'Alta: primeros 6 meses', 0, 30],
      ['academy2d', 'Tarifa Academy 2D', 0, 40],
      ['academy', 'Tarifa Academy', 0, 35],
      ['tariffS', 'Tarifa S / 9 clases', 0, 30],
      ['tariffM', 'Tarifa M / 13 clases', 0, 25],
      ['otherTariff', 'Otras tarifas', 0, 20],
      ['weeklyVeryLow', 'Media semanal <0,5', 0, 40],
      ['weeklyLow', 'Media semanal <1', 0, 30],
      ['failedPayment', 'Cada pago fallido', 0, 25],
      ['failedPaymentMax', 'Maximo pagos fallidos', 0, 50],
    ];
    const state = {
      rows: [],
      scoredRows: [],
      inactiveMembers: [],
      inactiveGeneratedAt: '',
      inactiveErrors: [],
      tariffCompletions: [],
      tariffGeneratedAt: '',
      tariffErrors: [],
      tariffQuery: '',
      tariffSortKey: 'cycle_start',
      tariffSortDir: 'desc',
      inactiveCenter: 'Getafe',
      inactiveQuery: '',
      inactiveWorkflow: 'pending',
      inactiveSortKey: 'days_without_class',
      inactiveSortDir: 'asc',
      scoredRows: [],
      injuries: [],
      injuryStatus: 'Todos',
      injuryCenter: 'Getafe',
      injuryQuery: '',
      injurySortKey: 'next_contact',
      injurySortDir: 'asc',
      deletedInjuries: [],
      deletedQuery: '',
      deletedSortKey: 'updated_at',
      deletedSortDir: 'desc',
      pendingQuery: '',
      pendingCenter: 'Getafe',
      pendingSortKey: 'days_remaining',
      pendingSortDir: 'asc',
      risk: 'Todos',
      query: '',
      sortKey: 'score',
      sortDir: 'desc',
      profile: 'equilibrado',
      weights: { ...profiles.equilibrado },
    };
    const $ = (id) => document.getElementById(id);

    async function loadData() {
      const response = await fetch('api/latest');
      const data = await response.json();
      state.rows = data.rows || [];
      state.inactiveMembers = data.inactive_members || [];
      state.inactiveGeneratedAt = data.inactive_members_generated_at || '';
      state.inactiveErrors = data.inactive_members_errors || [];
      state.tariffCompletions = data.tariff_completions || [];
      state.tariffGeneratedAt = data.tariff_completions_generated_at || '';
      state.tariffErrors = data.tariff_completions_errors || [];
      state.injuries = data.injuries_app || data.injuries || [];
      state.deletedInjuries = data.deleted_injuries || [];
      $('subtitle').textContent = data.generated_at
        ? `Última actualización: ${data.generated_at} · ${data.report_file}`
        : 'Todavia no hay informe. Pulsa Recalcular.';
      // Descarga CSV/Informe ocultas: el bloque superior queda solo con Recalcular.
      recalculateScores();
      renderRows();
    }

    function recalculateScores() {
      state.scoredRows = state.rows.map((row) => scoreRow(row));
      const total = state.scoredRows.length || 1;
      $('kpiListed').textContent = state.scoredRows.length;
      $('kpiHigh').textContent = state.scoredRows.filter((row) => row._risk === 'Alto').length;
      $('kpiMedium').textContent = state.scoredRows.filter((row) => row._risk === 'Medio').length;
      $('kpiLow').textContent = state.scoredRows.filter((row) => row._risk === 'Bajo').length;
      $('kpiNoClass').textContent = state.scoredRows.filter((row) => numberValue(row.dias_sin_clase) > 30).length;
      $('kpiInjured').textContent = state.injuries.length;
      $('kpiInjuryDue').textContent = pendingItems().length;
      $('kpiInactive').textContent = state.inactiveMembers.length;
      const avg = state.scoredRows.reduce((sum, row) => sum + row._score, 0) / total;
      $('kpiAvg').textContent = Math.round(avg * 10) / 10;
      renderPending();
      renderInjuries();
      renderInactive();
      renderTariffs();
      renderDeleted();
    }

    function scoreRow(row) {
      const w = state.weights;
      const days = numberValue(row.dias_sin_clase);
      const ageMonths = monthsSince(row.fecha_alta);
      const weekly = decimalValue(row.media_semanal);
      const failed = numberValue(row.pagos_fallidos_120d);
      const parts = [];
      let score = 0;

      if (Number.isNaN(days)) {
        score += w.noBooking;
        if (w.noBooking) parts.push('sin reserva registrada');
      } else if (days > 30) {
        score += w.days30; parts.push(`${days} dias sin clase`);
      } else if (days > 21) {
        score += w.days21; parts.push(`${days} dias sin clase`);
      } else if (days > 14) {
        score += w.days14; parts.push(`${days} dias sin clase`);
      } else if (days > 7) {
        score += w.days7; parts.push(`${days} dias sin clase`);
      }

      if (!Number.isNaN(ageMonths)) {
        if (ageMonths <= 1) { score += w.age1; if (w.age1) parts.push('alta reciente'); }
        else if (ageMonths <= 3) { score += w.age3; if (w.age3) parts.push('primeros 3 meses'); }
        else if (ageMonths <= 6) { score += w.age6; if (w.age6) parts.push('primeros 6 meses'); }
      }

      const tariffPoints = tariffScore(row.tarifa || '');
      score += tariffPoints;
      if (tariffPoints) parts.push(`tarifa ${row.tarifa}`);

      if (!Number.isNaN(weekly)) {
        if (weekly < 0.5) { score += w.weeklyVeryLow; if (w.weeklyVeryLow) parts.push('media semanal muy baja'); }
        else if (weekly < 1) { score += w.weeklyLow; if (w.weeklyLow) parts.push('media semanal baja'); }
      }

      if (failed > 0) {
        const failedPoints = Math.min(failed * w.failedPayment, w.failedPaymentMax);
        score += failedPoints;
        if (failedPoints) parts.push(`${failed} pagos fallidos`);
      }

      score = Math.min(Math.round(score), 100);
      const risk = score >= w.highThreshold ? 'Alto' : score >= w.mediumThreshold ? 'Medio' : 'Bajo';
      return { ...row, _score: score, _risk: risk, _motivos: parts.join(', ') || 'sin señales fuertes' };
    }

    function tariffScore(value) {
      const v = String(value || '').trim().toLowerCase();
      if (!v) return 0;
      if (v.includes('academy 2')) return state.weights.academy2d;
      if (v.includes('academy')) return state.weights.academy;
      if (v === 's' || v === 'tarifa s' || v.includes('9 clases')) return state.weights.tariffS;
      if (v === 'm' || v === 'tarifa m' || v.includes('13 clases')) return state.weights.tariffM;
      if (v === 'l' || v === 'xl' || v === 'tarifa l' || v === 'tarifa xl') return 0;
      return state.weights.otherTariff;
    }

    function clientKey(data) {
      const center = String(data.center || data.centro || '').trim().toLowerCase();
      const external = String(data.external_id || data.id || '').trim();
      const phone = String(data.phone || data.telefono || '').replace(/\D+/g, '');
      const email = String(data.email || '').trim().toLowerCase();
      const name = String(data.name || data.nombre || '').trim().toLowerCase();
      if (external) return `id:${center}:${external}`;
      if (phone) return `phone:${phone}`;
      if (email) return `email:${email}`;
      return `name:${center}:${name}`;
    }
    function riskClient(row) {
      return { name: row.nombre || '', phone: row.telefono || '', email: row.email || '', center: row.centro || row.center || row.sede || '', external_id: row.id || '', source: 'risk' };
    }
    function normalizePhone(value) {
      return String(value || '').replace(/\D+/g, '');
    }
    function findClientIdentityMatch(item) {
      const phone = normalizePhone(item.phone || item.telefono || '');
      const name = normalizeText(item.name || item.nombre || '');
      const center = normalizeText(item.center || item.centro || item.sede || '');
      const sources = [
        ...(state.inactiveMembers || []).map((row) => ({
          name: row.name || '', phone: row.phone || '', email: row.email || '', center: row.center || '', external_id: row.id || row.external_id || '', source: 'inactividad-aimharder'
        })),
        ...(state.rows || []).map((row) => ({
          name: row.nombre || row.name || '', phone: row.telefono || row.phone || '', email: row.email || '', center: row.centro || row.center || row.sede || '', external_id: row.id || row.external_id || '', source: 'risk'
        })),
      ];
      if (phone) {
        const byPhone = sources.find((row) => normalizePhone(row.phone) === phone && (row.email || row.external_id));
        if (byPhone) return byPhone;
      }
      if (name) {
        const byNameCenter = sources.find((row) => normalizeText(row.name) === name && (!center || normalizeText(row.center) === center) && (row.email || row.external_id));
        if (byNameCenter) return byNameCenter;
      }
      return {};
    }
    function injuryClient(item) {
      const match = findClientIdentityMatch(item);
      return { name: item.name || match.name || '', phone: item.phone || match.phone || '', email: item.email || match.email || '', center: item.center || match.center || '', external_id: item.external_id || match.external_id || '', registry_id: item.registro_id || item.registry_id || '', injury_type: item.type || item.injury_type || '', injury_label: item.label || '', injury_description: item.description || '', membership_name: item.membership_name || item.tariff || '', tariff: item.tariff || item.membership_name || '', last_membership_payment_date: item.last_membership_payment_date || '', source: 'risk-lesionados' };
    }
    function clientDataAttr(data) {
      const enriched = { ...data, client_key: clientKey(data) };
      return safe(JSON.stringify(enriched));
    }

    function normalizeText(value) {
      return String(value || '').normalize('NFD').replace(/[̀-ͯ]/g, '').trim().toLowerCase();
    }

    function renderRows() {
      const query = state.query.trim().toLowerCase();
      const filtered = state.scoredRows.filter((row) => {
        const riskOk = state.risk === 'Todos' || row._risk === state.risk;
        const haystack = `${row.nombre} ${row.telefono} ${row.email} ${row._motivos} ${row.tarifa}`.toLowerCase();
        return riskOk && (!query || haystack.includes(query));
      }).sort(compareRows);
      updateSortHeaders('[data-sort]', state.sortKey, state.sortDir);
      $('countLabel').textContent = `${filtered.length} visibles`;
      $('empty').hidden = filtered.length !== 0;
      $('rows').innerHTML = filtered.map(row => `
        <tr>
          <td class="score">${safe(row._score)}<span class="risk ${riskClass(row._risk)}">${safe(row._risk)}</span></td>
          <td><button class="client-link" type="button" data-client='${clientDataAttr(riskClient(row))}'>${safe(row.nombre)}</button><div class="muted">ID ${safe(row.id)}</div></td>
          <td class="contact">${safe(row.telefono)}<br>${safe(row.email)}</td>
          <td>${safe(row.dias_sin_clase)}</td>
          <td>${safe(formatDateEs(row.ultimo_pago_tarifa) || row.ultimo_pago_tarifa || 'Sin datos')}</td>
          <td>${safe(row.tarifa || 'Sin datos')}</td>
          <td class="reasons">${safe(row._motivos)}</td>
        </tr>
      `).join('');
    }

    function pendingItems() {
      return state.injuries.filter((item) => {
        const days = Number(item.days_remaining);
        const follow = normalizeText(item.follow_up || '');
        const noFollowUp = ['no', 'n', 'false', '0', 'sin seguimiento'].includes(follow);
        if (noFollowUp || item.status === 'Sin seguimiento') return false;
        return item.status === 'Pendiente respuesta' || item.status === 'Sin fecha' || (Number.isFinite(days) && days < 7);
      });
    }

    function injuryActionMenu(id, clientData = '') {
      const disabled = id ? '' : 'disabled';
      return `<button class="action-menu-trigger" type="button" aria-haspopup="menu" aria-expanded="false" data-registry-id="${safe(id)}" data-client='${clientData}' ${disabled}>Acciones</button>`;
    }

    function renderPending() {
      const query = state.pendingQuery.trim().toLowerCase();
      const filtered = pendingItems().filter((item) => {
        const searching = Boolean(query);
        const centerOk = searching || item.center === state.pendingCenter;
        const haystack = `${item.center} ${item.name} ${item.phone} ${item.label} ${item.description} ${item.latest_note} ${item.source}`.toLowerCase();
        return centerOk && (!query || haystack.includes(query));
      }).sort(comparePending);
      updateSortHeaders('[data-pending-sort]', state.pendingSortKey, state.pendingSortDir);
      $('pendingCountLabel').textContent = `${filtered.length} pendientes`;
      $('pendingEmpty').hidden = filtered.length !== 0;
      $('pendingRows').innerHTML = filtered.map((item) => {
        const id = item.registro_id || item.registry_id || '';
        return `
        <tr>
          <td><span class="risk ${statusClass(item.status)}">${safe(item.status)}</span><div class="muted">${safe(daysText(item.days_remaining))}</div></td>
          <td><span class="risk Bajo">${safe(item.center || '')}</span></td>
          <td><button class="client-link" type="button" data-client='${clientDataAttr(injuryClient(item))}'>${safe(item.name)}</button><div class="contact">${safe(item.phone)}${ghlButton(item.phone)}</div></td>
          <td><div class="name">${safe(item.label || '')}</div><div class="muted">${safe(item.description || '')}</div></td>
          <td>${safe(formatDateEs(item.next_contact) || 'Sin fecha')}</td>
          <td class="reasons">${safe(item.latest_note || '')}</td>
          <td>${injuryActionMenu(id, clientDataAttr(injuryClient(item)))}</td>
        </tr>`;
      }).join('');
    }

    function renderInjuries() {
      const query = state.injuryQuery.trim().toLowerCase();
      const filtered = state.injuries.filter((item) => {
        const searching = Boolean(query);
        const follow = normalizeText(item.follow_up || '');
        const noFollowUp = ['no', 'n', 'false', '0', 'sin seguimiento'].includes(follow) || item.status === 'Sin seguimiento';
        const rawDays = item.days_remaining;
        const days = Number(rawDays);
        const hasDays = rawDays !== null && rawDays !== undefined && rawDays !== '';
        const todayOrSoon = hasDays && Number.isFinite(days) && days >= 0 && days < 7;
        const overdue = item.status === 'Vencido' || (hasDays && Number.isFinite(days) && days < 0);
        const pendingToWrite = !noFollowUp && (overdue || todayOrSoon);
        const hasInteraction = Boolean(String(item.latest_note || '').trim());
        const statusOk = searching || (state.injuryStatus === 'Nuevos' ? (!noFollowUp && !hasInteraction) : (state.injuryStatus === 'Sin seguimiento' ? noFollowUp : (state.injuryStatus === 'Pendientes de escribir' ? pendingToWrite : (state.injuryStatus === 'Todos' ? !noFollowUp : (!noFollowUp && item.status === state.injuryStatus)))));
        const centerOk = searching || item.center === state.injuryCenter;
        const haystack = `${item.center} ${item.name} ${item.phone} ${item.label} ${item.description} ${item.latest_note} ${item.source} ${item.follow_up}`.toLowerCase();
        return statusOk && centerOk && (!query || haystack.includes(query));
      }).sort(compareInjuries);
      updateSortHeaders('[data-injury-sort]', state.injurySortKey, state.injurySortDir);
      $('injuryCountLabel').textContent = `${filtered.length} visibles`;
      $('injuryEmpty').hidden = filtered.length !== 0;
      $('injuryRows').innerHTML = filtered.map((item) => {
        const id = item.registro_id || item.registry_id || '';
        return `
        <tr>
          <td><span class="risk Bajo">${safe(item.center || '')}</span></td>
          <td><span class="risk ${statusClass(item.status)}">${safe(item.status)}</span><div class="muted">${safe(daysText(item.days_remaining))}</div></td>
          <td><button class="client-link" type="button" data-client='${clientDataAttr(injuryClient(item))}'>${safe(item.name)}</button><div class="contact">${safe(item.phone)}${ghlButton(item.phone)}</div><div class="mobile-injury-summary"><div class="mobile-meta"><span class="risk ${statusClass(item.status)}">${safe(item.status)}</span><span>${safe(item.phone || '')}</span><span>${safe(item.membership_name || item.tariff || 'Sin tarifa')}</span><span>${safe(formatDateEs(item.next_contact) || 'Sin fecha')}</span></div><div class="mobile-desc">${safe(item.description || '')}</div><div class="mobile-note">${safe(item.latest_note || '')}</div></div></td>
          <td><span class="risk ${item.membership_name || item.tariff ? 'Bajo' : 'Sin.fecha'}">${safe(item.membership_name || item.tariff || 'Sin tarifa')}</span><div class="muted">${safe(item.last_membership_payment_date ? 'último pago ' + formatDateEs(item.last_membership_payment_date) : '')}</div></td>
          <td>${safe(item.type || '')}</td>
          <td><span class="risk ${item.label ? 'Bajo' : 'Sin.fecha'}">${item.label ? 'Sí' : 'No'}</span><div class="muted">${safe(item.label || 'Sin etiqueta')}</div></td>
          <td><div class="name" title="${safe(item.description || '')}">${safe(item.description || '')}</div></td>
          <td>${safe(formatDateEs(item.next_contact) || 'Sin fecha')}</td>
          <td class="reasons" title="${safe(item.latest_note || '')}">${safe(item.latest_note || '')}</td>
          <td>${injuryActionMenu(id, clientDataAttr(injuryClient(item)))}</td>
        </tr>`;
      }).join('');
    }

    function renderInactive() {
      const query = state.inactiveQuery.trim().toLowerCase();
      const filtered = state.inactiveMembers.filter((item) => {
        const searching = Boolean(query);
        const centerOk = searching || item.center === state.inactiveCenter;
        const workflowOk = searching || state.inactiveWorkflow === 'all' || item.workflow_status === state.inactiveWorkflow;
        const haystack = `${item.center} ${item.name} ${item.phone} ${item.email} ${item.membership_name} ${item.bucket} ${item.workflow_status_label}`.toLowerCase();
        return centerOk && workflowOk && (!query || haystack.includes(query));
      }).sort(compareInactive);
      updateSortHeaders('[data-inactive-sort]', state.inactiveSortKey, state.inactiveSortDir);
      $('inactiveCountLabel').textContent = `${filtered.length} visibles`;
      $('inactiveEmpty').hidden = filtered.length !== 0;
      const meta = state.inactiveGeneratedAt ? `Última actualización: ${state.inactiveGeneratedAt}` : 'Pendiente de actualizar desde AimHarder.';
      const errors = state.inactiveErrors.length ? ` · Errores: ${state.inactiveErrors.join(' | ')}` : '';
      $('inactiveMeta').textContent = `${meta}${errors}`;
      $('inactiveRows').innerHTML = filtered.map((item) => {
        const clientData = clientDataAttr(inactiveClient(item));
        return `
        <tr>
          <td>${safe(item.center || '')}</td>
          <td>${safe(inactiveDaysText(item.days_without_class))}</td>
          <td><button class="client-link" type="button" data-client='${clientData}'>${safe(item.name)}</button><div class="contact">${safe(item.phone || '')}${ghlButton(item.phone)}${item.phone && item.email ? '<br>' : ''}${safe(item.email || '')}</div><div class="mobile-inactive-summary"><div class="mobile-meta"><span>${safe(inactiveDaysText(item.days_without_class))}</span><span>${safe(item.phone || item.email || 'Sin contacto')}</span><span>${safe(item.membership_name || 'Sin tarifa')}</span><span>${safe(formatDateEs(item.last_class_at) || 'Sin registro')}</span></div><div class="mobile-desc">${safe(inactiveSummary(item))}</div><div class="mobile-note">${safe(item.membership_active ? 'tarifa activa detectada' : 'tarifa no confirmada por pagos')}</div></div></td>
          <td><div class="name">${safe(item.membership_name || 'Sin datos')}</div></td>
          <td>${item.membership_active ? 'Sí' : 'No'}<div class="muted">${item.membership_active ? 'detectada por pagos' : 'no confirmada'}</div></td>
          <td>${safe(formatDateEs(item.last_class_at) || 'Sin registro')}</td>
          <td>${inactiveWorkflowActions(item, clientData)}</td>
        </tr>`;
      }).join('');
    }
    function inactiveWorkflowActions(item, clientData) {
      const payload = safe(JSON.stringify({ workflow_key: item.workflow_key || '', member: item }));
      if (item.workflow_status === 'pending') {
        return `<div class="inactive-actions"><button class="inactive-workflow-action" type="button" data-inactive-next="review" data-inactive-jump="review" data-inactive-payload='${payload}'>En revisión</button></div>`;
      }
      if (item.workflow_status === 'review') {
        return `<div class="inactive-actions"><button class="inactive-workflow-action" type="button" data-inactive-next="done" data-inactive-jump="done" data-inactive-payload='${payload}'>Hecho</button></div>`;
      }
      if (item.workflow_status === 'done') {
        const doneAt = formatDateEs(item.workflow_done_at) || 'Hecho';
        return `<div class="inactive-actions"><span class="muted">${safe(doneAt)}</span><button class="inactive-workflow-action" type="button" data-inactive-next="review" data-inactive-jump="review" data-inactive-payload='${payload}'>Reabrir</button></div>`;
      }
      return '';
    }
    function inactiveClient(item) {
      return { name: item.name || '', phone: item.phone || '', email: item.email || '', center: item.center || '', external_id: item.id || '', membership_name: item.membership_name || '', tariff: item.membership_name || '', last_membership_payment_date: item.last_membership_payment_date || '', source: 'inactividad-aimharder' };
    }
    function inactiveDaysText(value) {
      if (value === null || value === undefined || value === '') return 'sin clase registrada';
      return `${value} días`;
    }
    function inactiveSummary(item) {
      const lastClass = formatDateEs(item.last_class_at) || 'sin clase registrada';
      const days = inactiveDaysText(item.days_without_class);
      const tariff = item.membership_name || 'sin tarifa detectada';
      return `${days} sin venir · última clase: ${lastClass} · ${tariff}`;
    }

    function renderTariffs() {
      const query = state.tariffQuery.trim().toLowerCase();
      const filtered = state.tariffCompletions.filter((item) => {
        const haystack = `${item.center} ${item.name} ${item.phone} ${item.email} ${item.membership_name} ${item.status}`.toLowerCase();
        return !query || haystack.includes(query);
      }).sort(compareTariffs);
      updateSortHeaders('[data-tariff-sort]', state.tariffSortKey, state.tariffSortDir);
      $('tariffCountLabel').textContent = `${filtered.length} visibles`;
      $('tariffEmpty').hidden = filtered.length !== 0;
      const meta = state.tariffGeneratedAt ? `Última actualización: ${state.tariffGeneratedAt}` : 'Pendiente de actualizar desde AimHarder.';
      const errors = state.tariffErrors.length ? ` · ${state.tariffErrors.join(' | ')}` : '';
      $('tariffMeta').textContent = `${meta}${errors}`;
      $('tariffRows').innerHTML = filtered.map((item) => {
        const clientData = clientDataAttr(tariffClient(item));
        const consumed = item.consumed_classes === null || item.consumed_classes === undefined ? 'Pendiente' : item.consumed_classes;
        const remaining = item.remaining_classes === null || item.remaining_classes === undefined ? '—' : item.remaining_classes;
        return `
        <tr>
          <td><button class="client-link" type="button" data-client='${clientData}'>${safe(item.name)}</button><div class="contact">${safe(item.phone || '')}${item.phone && item.email ? '<br>' : ''}${safe(item.email || '')}</div></td>
          <td><div class="name">${safe(item.membership_name || 'Sin datos')}</div></td>
          <td>${safe(formatDateEs(item.cycle_start) || 'Sin datos')}</td>
          <td>${safe(item.contracted_classes || '—')}</td>
          <td>${safe(consumed)}</td>
          <td>${safe(remaining)}</td>
          <td>${safe(formatDateEs(item.last_class_at) || 'Sin registro')}</td>
          <td><span class="risk ${item.consumed_classes === null || item.consumed_classes === undefined ? 'Sin.fecha' : (item.remaining_classes <= 0 ? 'Alto' : 'Bajo')}">${safe(item.status || '')}</span><div class="muted">solo listado, sin avisos</div></td>
        </tr>`;
      }).join('');
    }
    function tariffClient(item) {
      return { name: item.name || '', phone: item.phone || '', email: item.email || '', center: item.center || 'Parla', external_id: item.id || '', source: 'tarifas-completadas' };
    }

    function renderDeleted() {
      const query = state.deletedQuery.trim().toLowerCase();
      const filtered = state.deletedInjuries.filter((item) => {
        const haystack = `${item.updated_at} ${item.center} ${item.name} ${item.phone} ${item.label} ${item.description} ${item.latest_note} ${item.source}`.toLowerCase();
        return !query || haystack.includes(query);
      }).sort(compareDeleted);
      updateSortHeaders('[data-deleted-sort]', state.deletedSortKey, state.deletedSortDir);
      $('deletedCountLabel').textContent = `${filtered.length} curados`;
      $('deletedEmpty').hidden = filtered.length !== 0;
      $('deletedRows').innerHTML = filtered.map((item) => `
        <tr>
          <td>${safe(formatDateEs(item.updated_at) || '')}</td>
          <td><span class="risk Bajo">${safe(item.center || '')}</span></td>
          <td><button class="client-link" type="button" data-client='${clientDataAttr(injuryClient(item))}'>${safe(item.name)}</button><div class="contact">${safe(item.phone)}${ghlButton(item.phone)}</div></td>
          <td><div class="name">${safe(item.label || '')}</div><div class="muted">${safe(item.description || '')}</div></td>
          <td class="reasons">${safe(item.latest_note || '')}</td>
        </tr>
      `).join('');
    }

    function safe(value) {
      return String(value ?? '').replace(/[&<>"']/g, (char) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
      }[char]));
    }
    function ghlButton(phone) {
      const p = String(phone || '').trim();
      if (!p) return '';
      return ` <a class="ghl-btn" href="api/ghl/open?phone=${encodeURIComponent(p)}" target="_blank" rel="noopener" title="Abrir conversación en GHL">↗ GHL</a>`;
    }
    function riskClass(value) {
      return safe(value).replace(/\s+/g, ' ');
    }
    function statusClass(value) {
      return safe(value).replace(/\s+/g, '.');
    }
    function daysText(value) {
      if (value === null || value === undefined || value === '') return '';
      const n = Number(value);
      if (n < 0) return `${Math.abs(n)} días vencido`;
      if (n === 0) return 'toca hoy';
      return `en ${n} días`;
    }
    function formatDateEs(value) {
      if (!value) return '';
      const raw = String(value).trim();
      if (!raw) return '';
      const iso = raw.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}:\d{2})(?::\d{2})?)?/);
      if (iso) return `${iso[3]}/${iso[2]}/${iso[1]}${iso[4] ? ' ' + iso[4] : ''}`;
      const slash = raw.match(/^(\d{1,2})[\/-](\d{1,2})[\/-](\d{2,4})(?:\s+(\d{1,2}:\d{2}))?/);
      if (slash) {
        const day = slash[1].padStart(2, '0');
        const month = slash[2].padStart(2, '0');
        const year = slash[3].length === 2 ? `20${slash[3]}` : slash[3];
        return `${day}/${month}/${year}${slash[4] ? ' ' + slash[4] : ''}`;
      }
      return raw;
    }
    function formatDateOnlyEs(value) {
      return formatDateEs(value).replace(/\s+\d{1,2}:\d{2}$/, '');
    }
    function noteMeta(note) {
      return [formatDateOnlyEs(note.created_at), note.author || ''].filter(Boolean).join(' · ');
    }
    function compareTariffs(a, b) {
      const dir = state.tariffSortDir === 'asc' ? 1 : -1;
      const key = state.tariffSortKey;
      if (['contracted_classes', 'consumed_classes', 'remaining_classes'].includes(key)) {
        const av = a[key] === null || a[key] === undefined || a[key] === '' ? -1 : Number(a[key]);
        const bv = b[key] === null || b[key] === undefined || b[key] === '' ? -1 : Number(b[key]);
        return (av - bv) * dir || textCompare(a.name, b.name, dir);
      }
      if (['cycle_start', 'last_class_at'].includes(key)) return dateCompare(a[key], b[key], dir) || textCompare(a.name, b.name, dir);
      return textCompare(a[key], b[key], dir) || textCompare(a.name, b.name, dir);
    }
    function compareInactive(a, b) {
      const dir = state.inactiveSortDir === 'asc' ? 1 : -1;
      const key = state.inactiveSortKey;
      let av = a[key] ?? '';
      let bv = b[key] ?? '';
      if (key === 'days_without_class') {
        av = av === null || av === undefined || av === '' ? -1 : Number(av);
        bv = bv === null || bv === undefined || bv === '' ? -1 : Number(bv);
        return (av - bv) * dir;
      }
      return String(av).localeCompare(String(bv)) * dir;
    }
    function compareRows(a, b) {
      const dir = state.sortDir === 'asc' ? 1 : -1;
      const key = state.sortKey;
      const numericKeys = new Set(['score', 'dias_sin_clase']);
      let av = key === 'score' ? a._score : a[key] ?? '';
      let bv = key === 'score' ? b._score : b[key] ?? '';
      if (numericKeys.has(key)) {
        av = Number.parseInt(av || '0', 10);
        bv = Number.parseInt(bv || '0', 10);
        return (av - bv) * dir;
      }
      if (key === 'ultimo_pago_tarifa') {
        return String(av).localeCompare(String(bv)) * dir;
      }
      return String(av).localeCompare(String(bv), 'es', { sensitivity: 'base' }) * dir;
    }
    function compareInjuries(a, b) {
      const dir = state.injurySortDir === 'asc' ? 1 : -1;
      const key = state.injurySortKey;
      if (key === 'days_remaining') return (numberOrInfinity(a.days_remaining) - numberOrInfinity(b.days_remaining)) * dir;
      if (key === 'status') return (statusRank(a.status) - statusRank(b.status)) * dir || textCompare(a.name, b.name, dir);
      if (key === 'next_contact') return dateCompare(a.next_contact, b.next_contact, dir) || textCompare(a.name, b.name, dir);
      return textCompare(a[key], b[key], dir) || textCompare(a.name, b.name, dir);
    }
    function comparePending(a, b) {
      const dir = state.pendingSortDir === 'asc' ? 1 : -1;
      const key = state.pendingSortKey;
      if (key === 'days_remaining') return (numberOrInfinity(a.days_remaining) - numberOrInfinity(b.days_remaining)) * dir || textCompare(a.name, b.name, dir);
      if (key === 'next_contact') return dateCompare(a.next_contact, b.next_contact, dir) || textCompare(a.name, b.name, dir);
      return textCompare(a[key], b[key], dir) || textCompare(a.name, b.name, dir);
    }
    function compareDeleted(a, b) {
      const dir = state.deletedSortDir === 'asc' ? 1 : -1;
      const key = state.deletedSortKey;
      if (key === 'updated_at') return dateCompare(a.updated_at, b.updated_at, dir) || textCompare(a.name, b.name, dir);
      return textCompare(a[key], b[key], dir) || textCompare(a.name, b.name, dir);
    }
    function updateSortHeaders(selector, activeKey, activeDir) {
      document.querySelectorAll(selector).forEach((button) => {
        const key = button.dataset.sort || button.dataset.injurySort || button.dataset.inactiveSort || button.dataset.tariffSort || button.dataset.pendingSort || button.dataset.deletedSort;
        button.classList.toggle('active', key === activeKey);
        button.classList.toggle('asc', key === activeKey && activeDir === 'asc');
        button.classList.toggle('desc', key === activeKey && activeDir === 'desc');
      });
    }
    function textCompare(a, b, dir) {
      return String(a ?? '').localeCompare(String(b ?? ''), 'es', { sensitivity: 'base', numeric: true }) * dir;
    }
    function numberOrInfinity(value) {
      const n = Number(value);
      return Number.isFinite(n) ? n : Number.POSITIVE_INFINITY;
    }
    function dateCompare(a, b, dir) {
      const av = dateSortValue(a);
      const bv = dateSortValue(b);
      return (av - bv) * dir;
    }
    function dateSortValue(value) {
      if (!value) return Number.POSITIVE_INFINITY;
      const text = String(value).trim();
      const m = text.match(/^(\d{1,2})[\/-](\d{1,2})[\/-](\d{2,4})$/);
      if (m) {
        const year = Number(m[3].length === 2 ? `20${m[3]}` : m[3]);
        return new Date(year, Number(m[2]) - 1, Number(m[1])).getTime();
      }
      const d = new Date(text);
      return Number.isNaN(d.getTime()) ? Number.POSITIVE_INFINITY : d.getTime();
    }
    function statusRank(value) {
      return { 'Vencido': 0, 'Sin fecha': 1, 'Hoy': 2, 'Pendiente respuesta': 3, 'Proximos 7 dias': 4, 'Al dia': 5 }[value] ?? 9;
    }
    function numberValue(value) {
      if (value === null || value === undefined || value === '') return NaN;
      return Number.parseInt(value, 10);
    }
    function decimalValue(value) {
      if (value === null || value === undefined || value === '') return NaN;
      return Number.parseFloat(String(value).replace(',', '.'));
    }
    function monthsSince(value) {
      if (!value) return NaN;
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return NaN;
      return Math.floor((Date.now() - date.getTime()) / (1000 * 60 * 60 * 24 * 30.44));
    }
    function renderControls() {
      $('configControls').innerHTML = controlDefs.map(([key, label, min, max]) => `
        <div class="slider-row">
          <div class="slider-head">
            <label for="ctrl-${key}">${safe(label)}</label>
            <span class="slider-value" id="value-${key}">${state.weights[key]}</span>
          </div>
          <input id="ctrl-${key}" type="range" min="${min}" max="${max}" value="${state.weights[key]}" data-weight="${key}">
        </div>
      `).join('');
      document.querySelectorAll('[data-weight]').forEach((input) => {
        input.addEventListener('input', () => {
          state.weights[input.dataset.weight] = Number.parseInt(input.value, 10);
          state.profile = 'personalizado';
          document.querySelectorAll('[data-profile]').forEach((button) => button.classList.remove('active'));
          $(`value-${input.dataset.weight}`).textContent = input.value;
          recalculateScores();
          renderRows();
        });
      });
    }
    function applyProfile(name) {
      state.profile = name;
      state.weights = { ...profiles[name] };
      document.querySelectorAll('[data-profile]').forEach((button) => {
        button.classList.toggle('active', button.dataset.profile === name);
      });
      renderControls();
      recalculateScores();
      renderRows();
    }


    let currentProfileClient = null;
    async function openClientProfile(client) {
      currentProfileClient = client;
      const response = await fetch('api/client-profile', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(client) });
      const profile = await response.json();
      renderClientProfile(profile);
      $('clientProfileModal').hidden = false;
    }
    function renderClientProfile(profile) {
      const client = profile.client || currentProfileClient || {};
      $('profileName').textContent = client.name || 'Perfil cliente';
      $('profileSubtitle').textContent = client.client_key || '';
      $('profilePhone').textContent = client.phone || '—';
      $('profileEmail').textContent = client.email || '—';
      $('profileCenter').textContent = client.center || '—';
      $('profileExternal').textContent = client.external_id || '—';
      $('profileTariff').textContent = client.membership_name || client.tariff || currentProfileClient?.membership_name || currentProfileClient?.tariff || '—';
      const injuryEdit = $('profileInjuryEdit');
      const registryId = client.registry_id || currentProfileClient?.registry_id || '';
      injuryEdit.hidden = !registryId;
      if (registryId) {
        injuryEdit.open = false;
        const injury = state.injuries.find((item) => (item.registro_id || item.registry_id || '') === registryId) || {};
        const typeValue = client.injury_type || injury.type || injury.injury_type || '';
        const descriptionValue = client.injury_description || injury.description || injury.label || '';
        const select = $('profileInjuryType');
        select.value = ['1', '2', '3'].includes(String(typeValue)) ? String(typeValue) : '1';
        $('profileInjuryDescription').value = descriptionValue || '';
        $('profileSaveInjury').dataset.registryId = registryId;
      }
      $('profileNotes').innerHTML = (profile.notes || []).map((note) => `<div class="profile-item">${safe(note.note)}<small>${safe(noteMeta(note))}</small></div>`).join('') || '<div class="muted">Sin notas todavía.</div>';
      $('profileEvents').innerHTML = (profile.events || []).map((event) => `<div class="profile-item"><strong>${safe(event.summary)}</strong><small>${safe(event.created_at)} · ${safe(event.event_type)} · ${safe(event.source_app || '')}</small></div>`).join('') || '<div class="muted">Sin historial todavía.</div>';
    }
    document.addEventListener('click', (event) => {
      const btn = event.target.closest('.client-link, .inactive-profile-trigger');
      if (!btn) return;
      event.preventDefault();
      try { openClientProfile(JSON.parse(btn.dataset.client || '{}')); } catch (err) { console.error(err); }
    });
    // Mobile injury cards are fully visible now; no leftover “desliza/desplegar” row toggle.
    document.addEventListener('click', (event) => {
      const row = event.target.closest('#injuriesPanel tbody tr');
      if (!row) return;
      if (event.target.closest('button, a, input, select, textarea, summary, details, #globalActionMenu')) return;
    });
    $('profileClose').addEventListener('click', () => { $('clientProfileModal').hidden = true; });
    $('clientProfileModal').addEventListener('click', (event) => { if (event.target.id === 'clientProfileModal') $('clientProfileModal').hidden = true; });
    $('profileSaveInjury').addEventListener('click', async () => {
      const button = $('profileSaveInjury');
      const registryId = button.dataset.registryId || currentProfileClient?.registry_id || '';
      if (!registryId) return;
      const injuryType = $('profileInjuryType').value.trim();
      const description = $('profileInjuryDescription').value.trim();
      if (!injuryType) { alert('Elige el tipo de lesión'); return; }
      const original = button.textContent;
      button.disabled = true;
      button.textContent = 'Guardando...';
      try {
        const response = await fetch('api/injury-details-update', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ registry_id: registryId, injury_type: injuryType, description }) });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'No se pudo guardar');
        await loadData();
        currentProfileClient = { ...(currentProfileClient || {}), injury_type: injuryType, injury_description: description, registry_id: registryId };
        const profileResponse = await fetch('api/client-profile', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(currentProfileClient) });
        renderClientProfile(await profileResponse.json());
        button.textContent = 'Guardado ✓';
        setTimeout(() => { button.textContent = original; button.disabled = false; }, 900);
      } catch (error) {
        button.disabled = false;
        button.textContent = original;
        alert(error.message);
      }
    });
    $('profileSaveNote').addEventListener('click', async () => {
      const note = $('profileNote').value.trim();
      if (!note || !currentProfileClient) return;
      const response = await fetch('api/client-note', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client: currentProfileClient, note, source_app: 'risk' }) });
      const profile = await response.json();
      $('profileNote').value = '';
      renderClientProfile(profile);
    });

    document.querySelectorAll('[data-risk]').forEach((button) => {
      button.addEventListener('click', () => {
    
    document.querySelectorAll('[data-risk]').forEach((b) => b.classList.remove('active'));
        button.classList.add('active');
        state.risk = button.dataset.risk;
        renderRows();
      });
    });
    document.querySelectorAll('[data-injury-status]').forEach((button) => {
      button.addEventListener('click', () => {
        document.querySelectorAll('[data-injury-status]').forEach((b) => b.classList.remove('active'));
        button.classList.add('active');
        state.injuryStatus = button.dataset.injuryStatus;
        renderInjuries();
      });
    });
    document.querySelectorAll('[data-injury-center]').forEach((button) => {
      button.addEventListener('click', () => {
        document.querySelectorAll('[data-injury-center]').forEach((b) => b.classList.remove('active'));
        button.classList.add('active');
        state.injuryCenter = button.dataset.injuryCenter;
        renderInjuries();
      });
    });
    document.querySelectorAll('[data-filter]').forEach((button) => {
      button.addEventListener('click', () => {
        const target = document.querySelector(`[data-risk="${button.dataset.filter}"]`);
        if (target) target.click();
      });
    });
    $('search').addEventListener('input', (event) => {
      state.query = event.target.value;
      renderRows();
    });
    $('injurySearch').addEventListener('input', (event) => {
      state.injuryQuery = event.target.value;
      renderInjuries();
    });
    $('pendingSearch').addEventListener('input', (event) => {
      state.pendingQuery = event.target.value;
      renderPending();
    });
    $('deletedSearch').addEventListener('input', (event) => {
      state.deletedQuery = event.target.value;
      renderDeleted();
    });
    $('inactiveSearch').addEventListener('input', (event) => {
      state.inactiveQuery = event.target.value;
      renderInactive();
    });
    $('tariffSearch').addEventListener('input', (event) => {
      state.tariffQuery = event.target.value;
      renderTariffs();
    });
    document.querySelectorAll('[data-inactive-center]').forEach((button) => {
      button.addEventListener('click', () => {
        document.querySelectorAll('[data-inactive-center]').forEach((b) => b.classList.remove('active'));
        button.classList.add('active');
        state.inactiveCenter = button.dataset.inactiveCenter;
        renderInactive();
      });
    });
    document.querySelectorAll('[data-inactive-workflow]').forEach((button) => {
      button.addEventListener('click', () => {
        document.querySelectorAll('[data-inactive-workflow]').forEach((b) => b.classList.remove('active'));
        button.classList.add('active');
        state.inactiveWorkflow = button.dataset.inactiveWorkflow;
        renderInactive();
      });
    });
    document.querySelectorAll('[data-pending-center]').forEach((button) => {
      button.addEventListener('click', () => {
        document.querySelectorAll('[data-pending-center]').forEach((b) => b.classList.remove('active'));
        button.classList.add('active');
        state.pendingCenter = button.dataset.pendingCenter;
        renderPending();
      });
    });
    document.querySelectorAll('[data-sort]').forEach((button) => {
      button.addEventListener('click', () => {
        if (state.sortKey === button.dataset.sort) {
          state.sortDir = state.sortDir === 'desc' ? 'asc' : 'desc';
        } else {
          state.sortKey = button.dataset.sort;
          state.sortDir = ['score', 'dias_sin_clase', 'ultimo_pago_tarifa'].includes(state.sortKey) ? 'desc' : 'asc';
        }
        renderRows();
      });
    });
    document.querySelectorAll('[data-injury-sort]').forEach((button) => {
      button.addEventListener('click', () => {
        if (state.injurySortKey === button.dataset.injurySort) {
          state.injurySortDir = state.injurySortDir === 'desc' ? 'asc' : 'desc';
        } else {
          state.injurySortKey = button.dataset.injurySort;
          state.injurySortDir = ['status', 'next_contact'].includes(state.injurySortKey) ? 'asc' : 'desc';
        }
        renderInjuries();
      });
    });
    document.querySelectorAll('[data-pending-sort]').forEach((button) => {
      button.addEventListener('click', () => {
        if (state.pendingSortKey === button.dataset.pendingSort) {
          state.pendingSortDir = state.pendingSortDir === 'desc' ? 'asc' : 'desc';
        } else {
          state.pendingSortKey = button.dataset.pendingSort;
          state.pendingSortDir = ['days_remaining', 'next_contact'].includes(state.pendingSortKey) ? 'asc' : 'desc';
        }
        renderPending();
      });
    });
    document.querySelectorAll('[data-inactive-sort]').forEach((button) => {
      button.addEventListener('click', () => {
        if (state.inactiveSortKey === button.dataset.inactiveSort) {
          state.inactiveSortDir = state.inactiveSortDir === 'desc' ? 'asc' : 'desc';
        } else {
          state.inactiveSortKey = button.dataset.inactiveSort;
          state.inactiveSortDir = 'asc';
        }
        renderInactive();
      });
    });
    document.querySelectorAll('[data-tariff-sort]').forEach((button) => {
      button.addEventListener('click', () => {
        if (state.tariffSortKey === button.dataset.tariffSort) {
          state.tariffSortDir = state.tariffSortDir === 'desc' ? 'asc' : 'desc';
        } else {
          state.tariffSortKey = button.dataset.tariffSort;
          state.tariffSortDir = ['cycle_start', 'contracted_classes', 'consumed_classes', 'remaining_classes', 'last_class_at'].includes(state.tariffSortKey) ? 'desc' : 'asc';
        }
        renderTariffs();
      });
    });
    async function promptInjuryNote(button) {
      const registryId = button.dataset.registryId;
      if (!registryId) return;
      const clientRaw = button.dataset.client || '';
      if (clientRaw) {
        try {
          closeActionMenu();
          await openClientProfile(JSON.parse(clientRaw));
          const modal = $('clientProfileModal');
          const card = modal?.querySelector('.profile-card-modal');
          const textarea = $('profileNote');
          if (card) card.scrollTop = 0;
          if (textarea) {
            textarea.focus({ preventScroll: false });
            textarea.scrollIntoView({ block: 'center', inline: 'nearest' });
          }
          return;
        } catch (err) { console.error(err); }
      }
      closeActionMenu(); // Prompt fallback also dismisses the menu before asking for text.
      const note = prompt('Añadir nota al seguimiento del lesionado:');
      if (!note || !note.trim()) return;
      await postInjuryAction(button, 'api/injury-followup-note', 'Añadir nota', null, { note: note.trim() });
    }

    async function postInjuryAction(button, endpoint, label, confirmText, extraPayload = {}) {
      const registryId = button.dataset.registryId;
      if (!registryId) return;
      closeActionMenu(); // Close the floating menu immediately after choosing an action.
      if (confirmText && !confirm(confirmText)) return;
      button.disabled = true;
      button.textContent = 'Guardando...';
      try {
        const response = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ registro_id: registryId, ...extraPayload }) });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'No se pudo guardar');
        await loadData();
      } catch (error) {
        alert(error.message);
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    }
    function ensureGlobalActionMenu() {
      let menu = document.getElementById('globalActionMenu');
      if (menu) return menu;
      menu = document.createElement('div');
      menu.id = 'globalActionMenu';
      menu.setAttribute('role', 'menu');
      menu.innerHTML = `<button class="button primary followup-done" type="button">Hecho</button><button class="button followup-note" type="button">Añadir nota</button><button class="button followup-pending" type="button">Pendiente respuesta</button><button class="button followup-remove" type="button">Curado / cerrar</button>`;
      document.body.appendChild(menu);
      return menu;
    }
    function closeActionMenu() {
      const menu = document.getElementById('globalActionMenu');
      if (menu) menu.classList.remove('open');
      document.querySelectorAll('.action-menu-trigger[aria-expanded="true"]').forEach((button) => button.setAttribute('aria-expanded', 'false'));
    }
    function openActionMenu(trigger) {
      const registryId = trigger.dataset.registryId;
      const clientData = trigger.dataset.client || '';
      if (!registryId) return;
      const menu = ensureGlobalActionMenu();
      menu.querySelectorAll('button').forEach((button) => { button.dataset.registryId = registryId; button.dataset.client = clientData; button.disabled = false; });
      document.querySelectorAll('.action-menu-trigger[aria-expanded="true"]').forEach((button) => button.setAttribute('aria-expanded', 'false'));
      trigger.setAttribute('aria-expanded', 'true');
      menu.classList.add('open');
      if (window.matchMedia('(max-width: 760px)').matches) return;
      const rect = trigger.getBoundingClientRect();
      const gap = 8;
      const menuWidth = Math.min(250, window.innerWidth - 24);
      const menuHeight = Math.min(menu.scrollHeight || 292, window.innerHeight - 24);
      let left = Math.min(Math.max(12, rect.right - menuWidth), window.innerWidth - menuWidth - 12);
      let top = rect.bottom + gap;
      if (top + menuHeight > window.innerHeight - 12) top = rect.top - menuHeight - gap;
      if (top < 12) top = Math.min(window.innerHeight - menuHeight - 12, 12);
      menu.style.left = `${left}px`;
      menu.style.top = `${top}px`;
      menu.style.bottom = 'auto';
    }
    document.addEventListener('click', (event) => {
      const trigger = event.target.closest('.action-menu-trigger');
      if (trigger) {
        event.preventDefault();
        event.stopPropagation();
        if (trigger.getAttribute('aria-expanded') === 'true') closeActionMenu(); else openActionMenu(trigger);
        return;
      }
      if (event.target.closest('#globalActionMenu')) return;
      closeActionMenu();
    });
    window.addEventListener('resize', closeActionMenu);
    window.addEventListener('scroll', closeActionMenu, true);
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeActionMenu(); });
    document.addEventListener('click', async (event) => {
      const doneButton = event.target.closest('.followup-done');
      if (doneButton) {
        await postInjuryAction(doneButton, 'api/injury-followup-done', 'Hecho');
        return;
      }
      const noteButton = event.target.closest('.followup-note');
      if (noteButton) {
        await promptInjuryNote(noteButton);
        return;
      }
      const pendingButton = event.target.closest('.followup-pending');
      if (pendingButton) {
        await postInjuryAction(pendingButton, 'api/injury-followup-pending-response', 'Pendiente respuesta', '¿Marcar como pendiente de respuesta? El lesionado seguirá activo.');
        return;
      }
      const rescheduleButton = event.target.closest('.followup-reschedule');
      if (rescheduleButton) {
        const days = Number(rescheduleButton.dataset.days || 1);
        const label = days === 1 ? 'Mañana' : '48h';
        await postInjuryAction(rescheduleButton, 'api/injury-followup-reschedule', label, `¿Reprogramar seguimiento para ${label}?`, { days });
        return;
      }
      const removeButton = event.target.closest('.followup-remove');
      if (removeButton) {
        await postInjuryAction(removeButton, 'api/injury-followup-remove', 'Curado / cerrar', '¿Marcar como curado/cerrar seguimiento? No se borra: quedará en Curados con su historial.');
      }
    });
    document.querySelectorAll('[data-deleted-sort]').forEach((button) => {
      button.addEventListener('click', () => {
        if (state.deletedSortKey === button.dataset.deletedSort) {
          state.deletedSortDir = state.deletedSortDir === 'desc' ? 'asc' : 'desc';
        } else {
          state.deletedSortKey = button.dataset.deletedSort;
          state.deletedSortDir = state.deletedSortKey === 'updated_at' ? 'desc' : 'asc';
        }
        renderDeleted();
      });
    });
    document.addEventListener('click', async (event) => {
      const button = event.target.closest('.inactive-workflow-action');
      if (!button) return;
      event.preventDefault();
      const original = button.textContent;
      button.disabled = true;
      button.textContent = 'Guardando...';
      try {
        const payload = JSON.parse(button.dataset.inactivePayload || '{}');
        payload.status = button.dataset.inactiveNext;
        const response = await fetch('api/inactive-workflow', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'No se pudo guardar');
        if (button.dataset.inactiveJump) {
          state.inactiveWorkflow = button.dataset.inactiveJump;
          document.querySelectorAll('[data-inactive-workflow]').forEach((b) => b.classList.toggle('active', b.dataset.inactiveWorkflow === state.inactiveWorkflow));
        }
        await loadData();
      } catch (error) {
        alert(error.message);
        button.disabled = false;
        button.textContent = original;
      }
    });
    document.querySelectorAll('[data-clear]').forEach((button) => {
      button.addEventListener('click', () => {
        if (button.dataset.clear === 'risk') {
          state.query = '';
          state.risk = 'Todos';
          $('search').value = '';
          document.querySelectorAll('[data-risk]').forEach((b) => b.classList.toggle('active', b.dataset.risk === 'Todos'));
          renderRows();
        } else if (button.dataset.clear === 'pending') {
          state.pendingQuery = '';
          state.pendingCenter = 'Getafe';
          $('pendingSearch').value = '';
          document.querySelectorAll('[data-pending-center]').forEach((b) => b.classList.toggle('active', b.dataset.pendingCenter === 'Getafe'));
          renderPending();
        } else if (button.dataset.clear === 'injuries') {
          state.injuryQuery = '';
          state.injuryCenter = 'Getafe';
          state.injuryStatus = 'Todos';
          $('injurySearch').value = '';
          document.querySelectorAll('[data-injury-center]').forEach((b) => b.classList.toggle('active', b.dataset.injuryCenter === 'Getafe'));
          document.querySelectorAll('[data-injury-status]').forEach((b) => b.classList.toggle('active', b.dataset.injuryStatus === 'Todos'));
          renderInjuries();
        } else if (button.dataset.clear === 'deleted') {
          state.deletedQuery = '';
          $('deletedSearch').value = '';
          renderDeleted();
        } else if (button.dataset.clear === 'inactive') {
          state.inactiveQuery = '';
          state.inactiveCenter = 'Getafe';
          state.inactiveWorkflow = 'pending';
          $('inactiveSearch').value = '';
          state.inactiveSortKey = 'days_without_class';
          state.inactiveSortDir = 'asc';
          document.querySelectorAll('[data-inactive-center]').forEach((b) => b.classList.toggle('active', b.dataset.inactiveCenter === 'Getafe'));
          document.querySelectorAll('[data-inactive-workflow]').forEach((b) => b.classList.toggle('active', b.dataset.inactiveWorkflow === 'pending'));
          renderInactive();
        } else if (button.dataset.clear === 'tariffs') {
          state.tariffQuery = '';
          $('tariffSearch').value = '';
          renderTariffs();
        }
      });
    });
    document.querySelectorAll('[data-tab]').forEach((button) => {
      button.addEventListener('click', () => {
        document.querySelectorAll('[data-tab]').forEach((b) => b.classList.remove('active'));
        button.classList.add('active');
        $('membersPanel').hidden = button.dataset.tab !== 'members';
        $('pendingPanel').hidden = button.dataset.tab !== 'pending';
        $('inactivePanel').hidden = button.dataset.tab !== 'inactive';
        $('tariffsPanel').hidden = button.dataset.tab !== 'tariffs';
        $('injuriesPanel').hidden = button.dataset.tab !== 'injuries';
        $('deletedPanel').hidden = button.dataset.tab !== 'deleted';
        $('rulesPanel').hidden = button.dataset.tab !== 'rules';
        $('settingsPanel').hidden = button.dataset.tab !== 'settings';
      });
    });
    document.querySelectorAll('[data-profile]').forEach((button) => {
      button.addEventListener('click', () => applyProfile(button.dataset.profile));
    });
    $('inactiveRefreshBtn').addEventListener('click', async () => {
      $('inactiveRefreshBtn').disabled = true;
      $('inactiveRefreshBtn').textContent = '↻ Actualizando...';
      try {
        const response = await fetch('api/inactive-refresh', { method: 'POST' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudo actualizar inactivos');
        await loadData();
      } catch (error) {
        alert(error.message);
      } finally {
        $('inactiveRefreshBtn').disabled = false;
        $('inactiveRefreshBtn').textContent = '↻ Actualizar inactivos';
      }
    });
    $('tariffRefreshBtn').addEventListener('click', async () => {
      $('tariffRefreshBtn').disabled = true;
      $('tariffRefreshBtn').textContent = '↻ Actualizando...';
      try {
        const response = await fetch('api/tariff-completions-refresh', { method: 'POST' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudo actualizar tarifas');
        state.tariffCompletions = data.rows || [];
        state.tariffGeneratedAt = data.generated_at || '';
        state.tariffErrors = data.errors || [];
        renderTariffs();
      } catch (error) {
        alert(error.message);
      } finally {
        $('tariffRefreshBtn').disabled = false;
        $('tariffRefreshBtn').textContent = '↻ Actualizar tarifas';
      }
    });
    $('refreshBtn').addEventListener('click', async () => {
      $('refreshBtn').disabled = true;
      $('refreshBtn').textContent = '↻ Actualizando...';
      try {
        const response = await fetch('api/refresh', { method: 'POST' });
        if (!response.ok) throw new Error('No se pudo recalcular');
        await loadData();
      } finally {
        $('refreshBtn').disabled = false;
        $('refreshBtn').textContent = '↻ Recalcular';
      }
    });
    renderControls();
    loadData();
  </script>

</body>
</html>"""


def main() -> int:
    settings = load_settings()
    server = ThreadingHTTPServer((settings.dashboard_host, settings.dashboard_port), DashboardHandler)
    print(f"Dashboard disponible en http://{settings.dashboard_host}:{settings.dashboard_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0
