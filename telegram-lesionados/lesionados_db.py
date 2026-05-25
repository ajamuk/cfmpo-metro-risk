#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

CONFIG = Path('/opt/telegram-lesionados/config.json')
CENTERS = ['Getafe', 'Parla', 'Las Rosas']
DB_DEFAULT = '/opt/telegram-lesionados/state/lesionados.sqlite'


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def db_path(config: dict | None = None) -> str:
    config = config or load_config()
    return config.get('sqliteDb') or DB_DEFAULT


def connect(config: dict | None = None) -> sqlite3.Connection:
    path = Path(db_path(config))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    init_db(con)
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.execute(
        '''
        CREATE TABLE IF NOT EXISTS injuries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          registry_id TEXT UNIQUE NOT NULL,
          center TEXT NOT NULL,
          name TEXT NOT NULL,
          phone TEXT,
          injury_type TEXT,
          label TEXT,
          follow_up TEXT,
          description TEXT,
          last_contact TEXT,
          next_contact TEXT,
          days_remaining TEXT,
          contact_1 TEXT,
          contact_2 TEXT,
          contact_3 TEXT,
          contact_4 TEXT,
          source TEXT,
          created_at TEXT,
          telegram_chat_id TEXT,
          telegram_message_id TEXT,
          active INTEGER NOT NULL DEFAULT 1,
          synced_to_sheet_at TEXT,
          updated_at TEXT NOT NULL
        )
        '''
    )
    con.execute('CREATE INDEX IF NOT EXISTS idx_injuries_center ON injuries(center)')
    con.execute('CREATE INDEX IF NOT EXISTS idx_injuries_name ON injuries(name)')
    con.execute('CREATE INDEX IF NOT EXISTS idx_injuries_next_contact ON injuries(next_contact)')
    con.commit()


def normalize_record(record: dict) -> dict:
    now = datetime.now().isoformat(timespec='seconds')
    center = str(record.get('center') or record.get('centro') or '').strip()
    name = str(record.get('name') or record.get('nombre') or '').strip()
    description = str(record.get('description') or record.get('lesion') or '').strip()
    created = str(record.get('created_at') or '').strip() or now
    telegram_chat = str(record.get('telegram_chat_id') or '').strip()
    telegram_msg = str(record.get('telegram_message_id') or '').strip()
    registry_id = str(record.get('registry_id') or record.get('registro_id') or '').strip()
    if not registry_id:
        if telegram_msg:
            registry_id = f'telegram:{center}:{telegram_chat}:{telegram_msg}'
        else:
            registry_id = f'local:{center}:{created}:{name}:{description}'
    follow_raw = record.get('follow_up') if 'follow_up' in record else record.get('seguimiento')
    if isinstance(follow_raw, bool):
        follow_up = 'Si' if follow_raw else 'No'
    else:
        follow_up = str(follow_raw or '').strip()
    label_raw = record.get('label') if 'label' in record else record.get('etiqueta')
    if isinstance(label_raw, bool):
        label = 'Sí' if label_raw else ''
    else:
        label = str(label_raw or '').strip()
    return {
        'registry_id': registry_id,
        'center': center,
        'name': name,
        'phone': str(record.get('phone') or record.get('telefono') or '').strip(),
        'injury_type': str(record.get('type') or record.get('injury_type') or record.get('tipo') or '').strip(),
        'label': label,
        'follow_up': follow_up,
        'description': description,
        'last_contact': str(record.get('last_contact') or record.get('fecha_ultimo_contacto') or '').strip(),
        'next_contact': str(record.get('next_contact') or record.get('proximo_contacto') or '').strip(),
        'days_remaining': '' if record.get('days_remaining') is None else str(record.get('days_remaining') or '').strip(),
        'contact_1': str(record.get('contact_1') or record.get('notas') or '').strip(),
        'contact_2': str(record.get('contact_2') or '').strip(),
        'contact_3': str(record.get('contact_3') or '').strip(),
        'contact_4': str(record.get('contact_4') or '').strip(),
        'source': str(record.get('source') or record.get('origen') or '').strip(),
        'created_at': created,
        'telegram_chat_id': telegram_chat,
        'telegram_message_id': telegram_msg,
    }


def upsert_injury(record: dict, con: sqlite3.Connection | None = None) -> tuple[bool, dict]:
    own = con is None
    con = con or connect()
    rec = normalize_record(record)
    if rec['center'] not in CENTERS:
        raise ValueError(f"Centro inválido: {rec['center']}")
    if not rec['name']:
        raise ValueError('Falta nombre')
    now = datetime.now().isoformat(timespec='seconds')
    existing = con.execute('SELECT id FROM injuries WHERE registry_id=?', (rec['registry_id'],)).fetchone()
    params = {**rec, 'updated_at': now}
    if existing:
        con.execute(
            '''
            UPDATE injuries SET center=:center,name=:name,phone=:phone,injury_type=:injury_type,label=:label,
              follow_up=:follow_up,description=:description,last_contact=:last_contact,next_contact=:next_contact,
              days_remaining=:days_remaining,contact_1=:contact_1,contact_2=:contact_2,contact_3=:contact_3,
              contact_4=:contact_4,source=:source,created_at=:created_at,telegram_chat_id=:telegram_chat_id,
              telegram_message_id=:telegram_message_id,updated_at=:updated_at
            WHERE registry_id=:registry_id
            ''',
            params,
        )
        created_new = False
    else:
        con.execute(
            '''
            INSERT INTO injuries(registry_id,center,name,phone,injury_type,label,follow_up,description,last_contact,
              next_contact,days_remaining,contact_1,contact_2,contact_3,contact_4,source,created_at,
              telegram_chat_id,telegram_message_id,updated_at)
            VALUES(:registry_id,:center,:name,:phone,:injury_type,:label,:follow_up,:description,:last_contact,
              :next_contact,:days_remaining,:contact_1,:contact_2,:contact_3,:contact_4,:source,:created_at,
              :telegram_chat_id,:telegram_message_id,:updated_at)
            ''',
            params,
        )
        created_new = True
    con.commit()
    if own:
        con.close()
    return created_new, rec


def list_injuries(center: str | None = None, con: sqlite3.Connection | None = None, active: int | None = 1) -> list[dict]:
    own = con is None
    con = con or connect()
    clauses = []
    params = []
    if active is not None:
        clauses.append('active=?')
        params.append(int(active))
    if center and center != 'Todos':
        clauses.append('center=?')
        params.append(center)
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = con.execute(f'SELECT * FROM injuries {where} ORDER BY active DESC, center, COALESCE(next_contact, ""), name', params).fetchall()
    out = [dict(r) for r in rows]
    if own:
        con.close()
    return out


def mark_synced(registry_id: str, con: sqlite3.Connection | None = None) -> None:
    own = con is None
    con = con or connect()
    con.execute('UPDATE injuries SET synced_to_sheet_at=? WHERE registry_id=?', (datetime.now().isoformat(timespec='seconds'), registry_id))
    con.commit()
    if own:
        con.close()
