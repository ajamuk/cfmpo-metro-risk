from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from .config import ROOT

WORKFLOW_PATH = ROOT / "reports" / "inactive_workflow.json"
ACTIVE_STATUSES = {"pending", "review", "done"}
STATUS_LABELS = {
    "pending": "Pendiente de escribir",
    "review": "En revisión",
    "done": "Hecho",
}


def load_inactive_workflow(path: Path = WORKFLOW_PATH) -> dict:
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "entries": {}}
    if not isinstance(data, dict):
        return {"version": 1, "entries": {}}
    data.setdefault("version", 1)
    entries = data.get("entries")
    if not isinstance(entries, dict):
        data["entries"] = {}
    return data


def save_inactive_workflow(data: dict, path: Path = WORKFLOW_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def member_key(row: dict) -> str:
    center = _clean(row.get("center"))
    client_id = _clean(row.get("id") or row.get("external_id"))
    if client_id:
        return f"id:{center}:{client_id}"
    phone = _digits(row.get("phone"))
    if phone:
        return f"phone:{center}:{phone}"
    email = _clean(row.get("email")).lower()
    if email:
        return f"email:{center}:{email}"
    return f"name:{center}:{_clean(row.get('name')).lower()}"


def cycle_marker(row: dict) -> str:
    """A new inactivity cycle starts when the last class changes.

    If someone was marked done and keeps not training, last_class_at is unchanged,
    so they do not return to pending. If they train again and later become inactive,
    AimHarder will report a newer last_class_at and this becomes a new cycle.
    """
    last_class = _clean(row.get("last_class_at"))
    if last_class:
        return f"last_class:{last_class}"
    return "last_class:none"


def enrich_inactive_rows(rows: list[dict], *, path: Path = WORKFLOW_PATH) -> list[dict]:
    workflow = load_inactive_workflow(path)
    entries: dict = workflow.setdefault("entries", {})
    now = _now()
    seen_keys: set[str] = set()
    changed = False

    enriched: list[dict] = []
    for row in rows:
        item = dict(row)
        key = member_key(item)
        marker = cycle_marker(item)
        seen_keys.add(key)
        entry = entries.get(key)

        if entry and entry.get("cycle_marker") != marker:
            # New real cycle: the member trained after the previous cycle and is now inactive again.
            entry = None

        eligible = _eligible_for_pending(item)
        if not entry:
            status = "pending" if eligible else "out_of_range"
            entry = {
                "member_key": key,
                "cycle_marker": marker,
                "status": status,
                "active_cycle": True,
                "cycle_started_at": now,
                "created_at": now,
                "updated_at": now,
            }
            entries[key] = entry
            changed = True
        else:
            if not entry.get("active_cycle", True):
                # Same key but previous cycle was closed. If marker did not change, keep it closed.
                pass
            entry["active_cycle"] = True
            entry["last_seen_at"] = now
            if entry.get("status") == "pending" and not eligible:
                entry["status"] = "out_of_range"
                entry["updated_at"] = now
                changed = True

        status = str(entry.get("status") or "out_of_range")
        item["workflow_key"] = key
        item["workflow_status"] = status
        item["workflow_status_label"] = STATUS_LABELS.get(status, "Fuera de flujo")
        item["workflow_written_at"] = entry.get("written_at", "")
        item["workflow_done_at"] = entry.get("done_at", "")
        item["workflow_cycle_marker"] = marker
        item["workflow_eligible"] = eligible
        enriched.append(item)

    # If someone disappears from the inactive cache, they trained again or stopped matching inactivity.
    # Close the active cycle so the next 8+ day absence can create a fresh pending cycle.
    for key, entry in list(entries.items()):
        if entry.get("active_cycle", False) and key not in seen_keys:
            entry["active_cycle"] = False
            entry["returned_at"] = now
            entry["updated_at"] = now
            changed = True

    if changed:
        workflow["updated_at"] = now
        save_inactive_workflow(workflow, path)
    return enriched


def mark_inactive_workflow(payload: dict, *, path: Path = WORKFLOW_PATH) -> dict:
    key = _clean(payload.get("workflow_key") or payload.get("member_key"))
    row = payload.get("member") if isinstance(payload.get("member"), dict) else {}
    if not key and row:
        key = member_key(row)
    if not key:
        raise ValueError("Falta socio")

    status = _clean(payload.get("status"))
    if status not in {"pending", "review", "done"}:
        raise ValueError("Estado no válido")

    workflow = load_inactive_workflow(path)
    entries: dict = workflow.setdefault("entries", {})
    now = _now()
    entry = entries.get(key) or {
        "member_key": key,
        "cycle_marker": cycle_marker(row) if row else _clean(payload.get("cycle_marker")),
        "active_cycle": True,
        "cycle_started_at": now,
        "created_at": now,
    }
    entry["status"] = status
    entry["active_cycle"] = True
    entry["updated_at"] = now
    if status == "review":
        entry.setdefault("written_at", now)
        entry.pop("done_at", None)
    elif status == "done":
        entry.setdefault("written_at", now)
        entry["done_at"] = now
    elif status == "pending":
        entry.pop("done_at", None)
    entries[key] = entry
    workflow["updated_at"] = now
    save_inactive_workflow(workflow, path)
    return {"ok": True, "workflow_key": key, "entry": entry}


def _eligible_for_pending(row: dict) -> bool:
    days = row.get("days_without_class")
    try:
        days_int = int(days)
    except Exception:
        return False
    return days_int > 7 and days_int < 40


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
