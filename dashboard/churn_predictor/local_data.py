from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, Optional


DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S")


@dataclass
class LocalSignals:
    membership_name: str = ""
    weekly_average: Optional[float] = None
    last_payment_date: Optional[date] = None
    last_membership_payment_date: Optional[date] = None
    failed_payments_120d: int = 0
    cancellation_requested: bool = False
    cancellation_date: Optional[date] = None
    cancellation_reason: str = ""


@dataclass
class SignalIndex:
    by_id: Dict[str, LocalSignals]
    by_email: Dict[str, LocalSignals]
    by_phone: Dict[str, LocalSignals]
    by_name: Dict[str, LocalSignals]

    def find(self, *, client_id: str = "", email: str = "", phone: str = "", name: str = "") -> LocalSignals:
        for key, mapping in (
            (client_id.strip(), self.by_id),
            (_norm_email(email), self.by_email),
            (_norm_phone(phone), self.by_phone),
            (_norm_name(name), self.by_name),
        ):
            if key and key in mapping:
                return mapping[key]
        return LocalSignals()


def load_signals(data_dir: Path) -> SignalIndex:
    signals: Dict[str, LocalSignals] = {}
    index = SignalIndex(by_id=signals, by_email={}, by_phone={}, by_name={})
    _load_memberships(data_dir / "tarifas.csv", signals)
    _load_payments(data_dir / "pagos.csv", index)
    _load_payments(data_dir / "pagos_getafe.csv", index)
    _load_payments(data_dir / "pagos_parla.csv", index)
    _load_payments(data_dir / "pagos_las_rosas.csv", index)
    _load_cancellations(data_dir / "cancelaciones.csv", signals)
    return index


def _signal(signals: Dict[str, LocalSignals], client_id: str) -> LocalSignals:
    return signals.setdefault(client_id, LocalSignals())


def _load_memberships(path: Path, signals: Dict[str, LocalSignals]) -> None:
    for row in _read_csv(path):
        client_id = _first(row, "id", "cliente_id", "client_id", "ID")
        if not client_id:
            continue
        signal = _signal(signals, client_id)
        signal.membership_name = _first(row, "tarifa", "membership", "membership_name", "Tarifa")
        weekly = _first(row, "media_semanal", "weekly_average", "Media Semanal")
        signal.weekly_average = _to_float(weekly)


def _load_payments(path: Path, index: SignalIndex) -> None:
    today = date.today()
    for row in _read_csv(path):
        signal = _payment_signal(index, row)
        paid_at = _payment_date(row)
        membership = _membership_from_payment(row)
        if paid_at and (signal.last_payment_date is None or paid_at > signal.last_payment_date):
            signal.last_payment_date = paid_at
        if membership and paid_at and (
            signal.last_membership_payment_date is None or paid_at > signal.last_membership_payment_date
        ):
            signal.last_membership_payment_date = paid_at
            if membership:
                signal.membership_name = membership
        status = _first(row, "estado", "status", "resultado", "Estado").lower()
        failed = any(word in status for word in ("fall", "devuelto", "rechaz", "impag", "failed", "unpaid"))
        if failed and paid_at and (today - paid_at).days <= 120:
            signal.failed_payments_120d += 1


def _load_cancellations(path: Path, signals: Dict[str, LocalSignals]) -> None:
    for row in _read_csv(path):
        client_id = _first(row, "id", "cliente_id", "client_id", "ID")
        if not client_id:
            continue
        signal = _signal(signals, client_id)
        signal.cancellation_requested = True
        signal.cancellation_date = _to_date(_first(row, "fecha", "fecha_cancelacion", "cancellation_date", "Fecha"))
        signal.cancellation_reason = _first(row, "motivo", "reason", "deactivation_reason", "Motivo")


def _read_csv(path: Path) -> Iterable[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
        return list(csv.DictReader(fh, dialect=dialect))


def _first(row: dict, *names: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        value = row.get(name)
        if value is None:
            value = lowered.get(name.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _to_float(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def _to_date(value: str) -> Optional[date]:
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value[:19], fmt).date()
        except ValueError:
            pass
    return None


def _payment_signal(index: SignalIndex, row: dict) -> LocalSignals:
    email = _norm_email(_first(row, "correo electronico", "correo electrónico", "email", "Correo electrónico"))
    phone = _norm_phone(_first(row, "telefono movil", "teléfono móvil", "mobile", "Teléfono móvil"))
    name = _norm_name(_first(row, "cliente", "name", "Cliente"))
    signal = None
    for key, mapping in ((email, index.by_email), (phone, index.by_phone), (name, index.by_name)):
        if key and key in mapping:
            signal = mapping[key]
            break
    if signal is None:
        signal = LocalSignals()
    if email:
        index.by_email[email] = signal
    if phone:
        index.by_phone[phone] = signal
    if name and name != "sin definir":
        index.by_name[name] = signal
    return signal


def _payment_date(row: dict) -> Optional[date]:
    explicit = _to_date(_first(row, "fecha", "payment_date", "date", "Fecha"))
    if explicit:
        return explicit
    status = _first(row, "estado", "status", "Estado")
    match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", status)
    return _to_date(match.group(1)) if match else None


def _membership_from_payment(row: dict) -> str:
    concept = _first(row, "concepto", "Concepto")
    value = re.sub(r"<[^>]+>", " ", concept).strip()
    value = re.sub(r"\s+", " ", value)
    match = re.match(
        r"^(CrossFit Academy 2 dias|CrossFit Academy 2 días|CrossFit Academy|Academy(?: 2D)?|Congelacion|Congelación|S|M|L|XL|Tarifa S|Tarifa M|Tarifa L|Tarifa XL|Bono 10 clases CrossFit Academy|Bono 10 clases)\b",
        value,
        re.I,
    )
    if not match:
        return ""
    membership = match.group(1)
    lowered = membership.lower()
    if lowered == "bono 10 clases crossfit academy":
        return "Bono 10 clases CrossFit Academy"
    if lowered == "bono 10 clases":
        return "Bono 10 clases"
    if "academy 2" in lowered:
        return "Academy 2D"
    if "academy" in lowered:
        return "Academy"
    return membership


def _norm_email(value: str) -> str:
    return value.strip().lower()


def _norm_phone(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _norm_name(value: str) -> str:
    stripped = " ".join((value or "").split()).lower()
    without_accents = "".join(
        c for c in unicodedata.normalize("NFKD", stripped) if not unicodedata.combining(c)
    )
    return without_accents
