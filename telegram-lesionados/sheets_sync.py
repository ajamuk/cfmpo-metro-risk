#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from lesionados_db import connect, list_injuries, mark_synced, upsert_injury

CONFIG = Path('/opt/telegram-lesionados/config.json')
DEFAULT_HEADERS = [
    'NOMBRE', 'Teléfono', 'Tipo de Lesión', 'Etiqueta', '¿Seguimiento?',
    'Descripción (Qué tiene)', 'Fecha Último Contacto', 'Próximo Contacto',
    'Días restantes', 'Contacto 1', 'Contacto 2', 'Contacto 3', 'Contacto 4',
    'Centro', 'Origen', 'Created At', 'Telegram Chat ID', 'Telegram Message ID', 'Registro ID',
    'Activo', 'Actualizado/Eliminado'
]
CENTERS = ['Getafe', 'Parla', 'Las Rosas']


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def sheets_service(config: dict | None = None):
    config = config or load_config()
    creds_path = config.get('googleServiceAccount') or '/opt/dashboard-facturacion/instance/google-service-account.json'
    creds = Credentials.from_service_account_file(
        creds_path,
        scopes=['https://www.googleapis.com/auth/spreadsheets'],
    )
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def spreadsheet_id(config: dict | None = None) -> str:
    config = config or load_config()
    sid = config.get('lesionadosSpreadsheetId') or ''
    if sid:
        return sid
    url = config.get('lesionadosSpreadsheetUrl') or ''
    m = re.search(r'/spreadsheets/d/([^/]+)', url)
    return m.group(1) if m else url.strip()


def beta_sheet_name(center: str, config: dict | None = None) -> str:
    config = config or load_config()
    return (config.get('betaSheets') or {}).get(center) or f'Beta {center}'


def ensure_beta_sheets(config: dict | None = None) -> None:
    config = config or load_config()
    svc = sheets_service(config)
    sid = spreadsheet_id(config)
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    existing = {s['properties']['title']: s['properties']['sheetId'] for s in meta.get('sheets', [])}
    requests = []
    for center in CENTERS:
        title = beta_sheet_name(center, config)
        if title not in existing:
            requests.append({'addSheet': {'properties': {'title': title}}})
    if requests:
        svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={'requests': requests}).execute()
    for center in CENTERS:
        title = beta_sheet_name(center, config)
        values = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"'{title}'!A1:U1").execute().get('values', [])
        current = values[0] if values else []
        if len(current) < len(DEFAULT_HEADERS) or current[:len(DEFAULT_HEADERS)] != DEFAULT_HEADERS:
            svc.spreadsheets().values().update(
                spreadsheetId=sid,
                range=f"'{title}'!A1:U1",
                valueInputOption='USER_ENTERED',
                body={'values': [DEFAULT_HEADERS]},
            ).execute()


def existing_keys(sheet: str, config: dict | None = None) -> set[str]:
    config = config or load_config()
    svc = sheets_service(config)
    sid = spreadsheet_id(config)
    rows = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"'{sheet}'!A:U").execute().get('values', [])
    keys = set()
    for row in rows[1:]:
        registro = row[18].strip() if len(row) > 18 else ''
        if registro:
            keys.add(f'id:{registro}')
        name = row[0].strip().lower() if len(row) > 0 else ''
        desc = row[5].strip().lower() if len(row) > 5 else ''
        created = row[15].strip() if len(row) > 15 else ''
        if name or desc or created:
            keys.add(f'row:{name}|{desc}|{created}')
    return keys


def normalize_source_row(row: list[str], center: str, headers: list[str]) -> list[str]:
    by_header = {str(h).strip().lower(): i for i, h in enumerate(headers)}

    def val(*names: str) -> str:
        for name in names:
            idx = by_header.get(name.strip().lower())
            if idx is not None and idx < len(row):
                return str(row[idx]).strip()
        return ''

    out = [
        val('NOMBRE', 'Nombre del Atleta'),
        val('Teléfono'),
        val('Tipo de Lesión'),
        val('Etiqueta'),
        val('¿Seguimiento?'),
        val('Descripción (Qué tiene)'),
        val('Fecha Último Contacto'),
        val('Próximo Contacto'),
        val('Días restantes'),
        val('Contacto 1'),
        val('Contacto 2'),
        val('Contacto 3'),
        val('Contacto 4'),
        center,
        'Hoja existente',
        '',
        '',
        '',
        f'existente:{center}:{val("NOMBRE", "Nombre del Atleta").lower()}:{val("Descripción (Qué tiene)").lower()}:{val("Fecha Último Contacto")}',
    ]
    return out


def import_existing_center(center: str, config: dict | None = None) -> int:
    config = config or load_config()
    svc = sheets_service(config)
    sid = spreadsheet_id(config)
    source_sid = config.get('sourceLesionadosSpreadsheetId') or sid
    target = beta_sheet_name(center, config)
    source_rows = svc.spreadsheets().values().get(spreadsheetId=source_sid, range=f"'{center}'!A:AD").execute().get('values', [])
    if not source_rows:
        return 0
    headers = source_rows[0]
    keys = existing_keys(target, config)
    to_append = []
    for row in source_rows[1:]:
        if not any(str(x).strip() for x in row):
            continue
        out = normalize_source_row(row, center, headers)
        if not out[0] or not out[5]:
            continue
        key = f'id:{out[18]}'
        if key in keys:
            # La fila ya existe en las hojas beta: no reimportar desde la hoja antigua,
            # porque eso pisa notas/acciones operativas ya guardadas en la beta/DB.
            continue
        record = {
            'registry_id': out[18], 'center': out[13], 'name': out[0], 'phone': out[1], 'injury_type': out[2],
            'label': out[3], 'follow_up': out[4], 'description': out[5], 'last_contact': out[6],
            'next_contact': out[7], 'days_remaining': out[8], 'contact_1': out[9], 'contact_2': out[10],
            'contact_3': out[11], 'contact_4': out[12], 'source': out[14], 'created_at': out[15],
            'telegram_chat_id': out[16], 'telegram_message_id': out[17],
        }
        upsert_injury(record)
        to_append.append(out)
        keys.add(key)
    if to_append:
        svc.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"'{target}'!A:S",
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': to_append},
        ).execute()
    return len(to_append)


def local_record_to_row(record: dict) -> list[str]:
    created = str(record.get('created_at') or '')
    date_str = ''
    try:
        date_str = datetime.fromisoformat(created).strftime('%d/%m/%Y')
    except Exception:
        date_str = created[:10]
    center = str(record.get('centro') or '').strip()
    name = str(record.get('nombre') or '').strip()
    desc = str(record.get('lesion') or '').strip()
    msg = str(record.get('telegram_message_id') or '')
    chat = str(record.get('telegram_chat_id') or '')
    rid = f'telegram:{center}:{chat}:{msg}' if msg else f'local:{center}:{created}:{name}:{desc}'
    return [
        name,
        '',
        '',
        'Sí' if record.get('etiqueta') else '',
        'Si' if record.get('seguimiento') else 'No',
        desc,
        date_str,
        '',
        '',
        str(record.get('notas') or '').strip(),
        '',
        '',
        '',
        center,
        'Telegram/Formulario',
        created,
        chat,
        msg,
        rid,
    ]


def append_record(record: dict, config: dict | None = None) -> bool:
    config = config or load_config()
    _created, normalized = upsert_injury(record)
    center = normalized.get('center') or ''
    if center not in CENTERS:
        return False
    ensure_beta_sheets(config)
    target = beta_sheet_name(center, config)
    row = db_record_to_sheet_row(normalized)
    keys = existing_keys(target, config)
    if f'id:{row[18]}' in keys:
        mark_synced(row[18])
        return False
    svc = sheets_service(config)
    svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id(config),
        range=f"'{target}'!A:S",
        valueInputOption='USER_ENTERED',
        insertDataOption='INSERT_ROWS',
        body={'values': [row]},
    ).execute()
    mark_synced(row[18])
    return True


def db_record_to_sheet_row(record: dict) -> list[str]:
    return [
        record.get('name', ''),
        record.get('phone', ''),
        record.get('injury_type', ''),
        record.get('label', ''),
        record.get('follow_up', ''),
        record.get('description', ''),
        record.get('last_contact', ''),
        record.get('next_contact', ''),
        record.get('days_remaining', ''),
        record.get('contact_1', ''),
        record.get('contact_2', ''),
        record.get('contact_3', ''),
        record.get('contact_4', ''),
        record.get('center', ''),
        record.get('source', ''),
        record.get('created_at', ''),
        record.get('telegram_chat_id', ''),
        record.get('telegram_message_id', ''),
        record.get('registry_id', ''),
        'Sí' if int(record.get('active', 1) or 0) == 1 else 'No',
        record.get('updated_at', '') or record.get('synced_to_sheet_at', ''),
    ]


def import_local_records(config: dict | None = None) -> int:
    config = config or load_config()
    path = Path(config.get('localJsonl') or '/opt/telegram-lesionados/state/registros.jsonl')
    if not path.exists():
        return 0
    added = 0
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if append_record(record, config):
            added += 1
    return added


def sheet_row_positions(sheet: str, config: dict | None = None) -> dict[str, int]:
    config = config or load_config()
    svc = sheets_service(config)
    sid = spreadsheet_id(config)
    rows = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"'{sheet}'!A:U").execute().get('values', [])
    positions: dict[str, int] = {}
    for idx, row in enumerate(rows[1:], start=2):
        registro = row[18].strip() if len(row) > 18 else ''
        if registro:
            positions[registro] = idx
    return positions


def sync_record_to_sheet(registry_id: str, config: dict | None = None) -> bool:
    config = config or load_config()
    registry_id = str(registry_id or '').strip()
    if not registry_id:
        return False
    con = connect(config)
    try:
        row = con.execute('SELECT * FROM injuries WHERE registry_id=?', (registry_id,)).fetchone()
        if not row:
            return False
        record = dict(row)
    finally:
        con.close()
    center = record.get('center') or ''
    if center not in CENTERS:
        return False
    ensure_beta_sheets(config)
    target = beta_sheet_name(center, config)
    svc = sheets_service(config)
    sid = spreadsheet_id(config)
    out = db_record_to_sheet_row(record)
    positions = sheet_row_positions(target, config)
    if registry_id in positions:
        svc.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"'{target}'!A{positions[registry_id]}:U{positions[registry_id]}",
            valueInputOption='USER_ENTERED',
            body={'values': [out]},
        ).execute()
    else:
        svc.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"'{target}'!A:U",
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': [out]},
        ).execute()
    mark_synced(registry_id)
    return True


def sync_db_to_sheets(config: dict | None = None) -> int:
    config = config or load_config()
    ensure_beta_sheets(config)
    svc = sheets_service(config)
    sid = spreadsheet_id(config)
    changed = 0
    for center in CENTERS:
        target = beta_sheet_name(center, config)
        positions = sheet_row_positions(target, config)
        batch_updates = []
        to_append = []
        to_mark = []
        for record in list_injuries(center, active=None):
            row = db_record_to_sheet_row(record)
            registry_id = row[18]
            if registry_id in positions:
                batch_updates.append({
                    'range': f"'{target}'!A{positions[registry_id]}:U{positions[registry_id]}",
                    'values': [row],
                })
                changed += 1
                to_mark.append(registry_id)
                continue
            to_append.append(row)
            to_mark.append(registry_id)
        if batch_updates:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={'valueInputOption': 'USER_ENTERED', 'data': batch_updates},
            ).execute()
        if to_append:
            svc.spreadsheets().values().append(
                spreadsheetId=sid,
                range=f"'{target}'!A:U",
                valueInputOption='USER_ENTERED',
                insertDataOption='INSERT_ROWS',
                body={'values': to_append},
            ).execute()
            changed += len(to_append)
        for registry_id in to_mark:
            mark_synced(registry_id)
    return changed


def setup_beta() -> dict:
    config = load_config()
    ensure_beta_sheets(config)
    imported = {center: import_existing_center(center, config) for center in CENTERS}
    local_added = import_local_records(config)
    db_to_sheet = sync_db_to_sheets(config)
    return {'imported_existing': imported, 'imported_local': local_added, 'db_to_sheet': db_to_sheet}


if __name__ == '__main__':
    print(json.dumps(setup_beta(), ensure_ascii=False, indent=2))
