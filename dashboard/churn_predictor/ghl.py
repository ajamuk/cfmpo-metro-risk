"""GoHighLevel integration: phone -> contactId -> conversationId, with SQLite cache.

Exposes:
    resolve_conversation(phone) -> dict {ok, conversation_url, contact_id, conversation_id, error?}

Lookup order:
    1. Local cache (ghl_contacts table, keyed by normalized phone).
    2. GHL API contact search (by phone, with default country code prefix if needed).
    3. GHL API conversation search for that contact; if none, create one.

Config (env vars or .env in dashboard/):
    GHL_PIT_TOKEN              Private Integration Token (pit-...)
    GHL_LOCATION_ID            Sub-account location id
    GHL_DOMAIN                 White-label domain (e.g. crm.nexor.digital). Defaults to app.gohighlevel.com.
    GHL_DEFAULT_COUNTRY_CODE   Country code digits to prepend if phone has no '+' (default: 34).
    GHL_CACHE_DB               Path to SQLite (default: /opt/telegram-lesionados/state/lesionados.sqlite).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional


GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"
DEFAULT_DB = "/opt/telegram-lesionados/state/lesionados.sqlite"


def _settings() -> dict:
    return {
        "token": os.environ.get("GHL_PIT_TOKEN", "").strip(),
        "location_id": os.environ.get("GHL_LOCATION_ID", "").strip(),
        "domain": os.environ.get("GHL_DOMAIN", "app.gohighlevel.com").strip() or "app.gohighlevel.com",
        "country_code": re.sub(r"\D+", "", os.environ.get("GHL_DEFAULT_COUNTRY_CODE", "34")) or "34",
        "db_path": os.environ.get("GHL_CACHE_DB", DEFAULT_DB).strip() or DEFAULT_DB,
    }


# -------------------- phone normalization --------------------

def normalize_phone(raw: str, default_country_code: str = "34") -> str:
    """Return E.164-ish string with leading '+', e.g. '+34600111222'.

    Rules:
        - Strip all non-digits except a leading '+'.
        - If already starts with '+', keep as-is.
        - Else if starts with '00', replace with '+'.
        - Else if starts with the default country code, prepend '+'.
        - Else prepend '+' + default country code.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("+"):
        digits = re.sub(r"\D+", "", s)
        return f"+{digits}" if digits else ""
    digits = re.sub(r"\D+", "", s)
    if not digits:
        return ""
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    if digits.startswith(default_country_code):
        return f"+{digits}"
    return f"+{default_country_code}{digits}"


def _phone_key(phone: str) -> str:
    """Cache key: digits only, no '+', no spaces."""
    return re.sub(r"\D+", "", phone or "")


# -------------------- SQLite cache --------------------

def _connect(db_path: str) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS ghl_contacts (
            phone_norm TEXT PRIMARY KEY,
            contact_id TEXT,
            conversation_id TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.commit()
    return con


def _cache_get(con: sqlite3.Connection, phone_norm: str) -> Optional[sqlite3.Row]:
    return con.execute(
        "SELECT phone_norm, contact_id, conversation_id, updated_at FROM ghl_contacts WHERE phone_norm=?",
        (phone_norm,),
    ).fetchone()


def _cache_upsert(con: sqlite3.Connection, phone_norm: str, contact_id: str, conversation_id: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    con.execute(
        """
        INSERT INTO ghl_contacts(phone_norm, contact_id, conversation_id, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(phone_norm) DO UPDATE SET
            contact_id=excluded.contact_id,
            conversation_id=excluded.conversation_id,
            updated_at=excluded.updated_at
        """,
        (phone_norm, contact_id or "", conversation_id or "", now),
    )
    con.commit()


# -------------------- GHL API --------------------

def _api_request(method: str, path: str, token: str, *, params: dict | None = None, body: dict | None = None, timeout: int = 10) -> dict:
    url = f"{GHL_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Version", GHL_API_VERSION)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8")
        except Exception:
            err_body = ""
        raise RuntimeError(f"GHL API {method} {path} HTTP {exc.code}: {err_body[:300]}") from exc


def _phone_variants(phone_e164: str, country_code: str = "34") -> list[str]:
    """Generate plausible stored formats of a phone for GHL lookup.

    GHL contacts may have been imported/created with different formats. We try:
        +34644355820  (E.164)
        34644355820   (digits only, with country code)
        644355820     (national, no country code)
    """
    variants: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    add(phone_e164)
    digits = re.sub(r"\D+", "", phone_e164)
    add(digits)
    if country_code and digits.startswith(country_code):
        add(digits[len(country_code):])
    return variants


def _find_contact_id(phone_e164: str, token: str, location_id: str, country_code: str = "34") -> Optional[str]:
    # The /contacts/search/duplicate endpoint is built for "does this contact already exist"
    # lookups and accepts a phone number. Falls back to /contacts/search if needed.
    # We try multiple phone formats because contacts may be stored without '+' or country code.
    for variant in _phone_variants(phone_e164, country_code):
        try:
            resp = _api_request(
                "GET",
                "/contacts/search/duplicate",
                token,
                params={"locationId": location_id, "number": variant},
            )
            contact = resp.get("contact") or {}
            cid = contact.get("id")
            if cid:
                return cid
        except RuntimeError:
            pass

    # Fallback: generic search by query string (also try variants).
    for variant in _phone_variants(phone_e164, country_code):
        try:
            resp = _api_request(
                "POST",
                "/contacts/search",
                token,
                body={
                    "locationId": location_id,
                    "query": variant,
                    "pageLimit": 5,
                },
            )
            contacts = resp.get("contacts") or []
            if contacts:
                return contacts[0].get("id")
        except RuntimeError:
            continue
    return None


def _find_or_create_conversation_id(contact_id: str, token: str, location_id: str) -> Optional[str]:
    try:
        resp = _api_request(
            "GET",
            "/conversations/search",
            token,
            params={"locationId": location_id, "contactId": contact_id, "limit": 1},
        )
        convs = resp.get("conversations") or []
        if convs:
            return convs[0].get("id")
    except RuntimeError:
        pass

    # No existing conversation: create one so the deep link works.
    try:
        resp = _api_request(
            "POST",
            "/conversations/",
            token,
            body={"locationId": location_id, "contactId": contact_id},
        )
        conv = resp.get("conversation") or resp
        return conv.get("id")
    except RuntimeError:
        return None


# -------------------- public API --------------------

def conversation_url(domain: str, location_id: str, contact_id: str) -> str:
    return f"https://{domain}/v2/location/{location_id}/contacts/detail/{contact_id}"


def resolve_conversation(phone: str) -> dict:
    """Main entry point. Returns dict with 'ok' and either 'conversation_url' or 'error'."""
    cfg = _settings()
    if not cfg["token"] or not cfg["location_id"]:
        return {"ok": False, "error": "GHL no configurado (faltan GHL_PIT_TOKEN o GHL_LOCATION_ID)"}

    phone_e164 = normalize_phone(phone, cfg["country_code"])
    if not phone_e164:
        return {"ok": False, "error": "Teléfono vacío o inválido"}

    key = _phone_key(phone_e164)
    con = _connect(cfg["db_path"])
    try:
        cached = _cache_get(con, key)
        contact_id = cached["contact_id"] if cached else ""

        if not contact_id:
            contact_id = _find_contact_id(phone_e164, cfg["token"], cfg["location_id"], cfg["country_code"]) or ""
            if not contact_id:
                return {"ok": False, "error": f"Contacto no encontrado en GHL para {phone_e164}"}
            _cache_upsert(con, key, contact_id, "")
    finally:
        con.close()

    return {
        "ok": True,
        "contact_id": contact_id,
        "conversation_url": conversation_url(cfg["domain"], cfg["location_id"], contact_id),
    }
