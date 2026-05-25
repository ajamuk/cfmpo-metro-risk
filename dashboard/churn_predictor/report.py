from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List

from .scoring import ScoredClient


HEADERS = [
    "score",
    "riesgo",
    "motivos",
    "id",
    "nombre",
    "sede",
    "telefono",
    "email",
    "fecha_alta",
    "ultima_clase",
    "dias_sin_clase",
    "tarifa",
    "media_semanal",
    "ultimo_pago",
    "ultimo_pago_tarifa",
    "pagos_fallidos_120d",
    "cancelacion_solicitada",
]


def write_reports(rows: List[ScoredClient], report_dir: Path, min_score: int) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    filtered = [
        row for row in rows
        if row.risk != "Baja real" and _has_current_membership(row)
    ]
    csv_path = report_dir / f"posibles_bajas_{today}.csv"
    html_path = report_dir / f"posibles_bajas_{today}.html"
    _write_csv(filtered, csv_path)
    _write_html(filtered, html_path)
    return csv_path, html_path


def _write_csv(rows: Iterable[ScoredClient], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADERS)
        for row in rows:
            writer.writerow(_row_values(row))


def _write_html(rows: List[ScoredClient], path: Path) -> None:
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    body_rows = "\n".join(
        "<tr>"
        + "".join(f"<td>{_escape(str(value))}</td>" for value in _row_values(row))
        + "</tr>"
        for row in rows
    )
    path.write_text(
        f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Posibles bajas - CrossFit MetroPolitano</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 28px; color: #191919; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    .meta {{ color: #666; margin-bottom: 22px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #121212; color: #87B15F; position: sticky; top: 0; }}
    tr:nth-child(even) {{ background: #f8f8f8; }}
    td:first-child {{ font-weight: 700; }}
  </style>
</head>
<body>
  <h1>Posibles bajas</h1>
  <div class="meta">CrossFit MetroPolitano - generado el {generated}</div>
  <table>
    <thead><tr>{''.join(f'<th>{h}</th>' for h in HEADERS)}</tr></thead>
    <tbody>{body_rows}</tbody>
  </table>
</body>
</html>
""",
        encoding="utf-8",
    )


def _row_values(row: ScoredClient) -> list:
    return [
        row.score,
        row.risk,
        row.reasons,
        row.client_id,
        row.name,
        row.center,
        row.phone,
        row.email,
        row.created_at,
        row.last_booking_at,
        "" if row.days_without_class is None else row.days_without_class,
        row.membership_name,
        "" if row.weekly_average is None else row.weekly_average,
        row.last_payment_date,
        row.last_membership_payment_date,
        row.failed_payments_120d,
        "si" if row.cancellation_requested else "no",
    ]


def _has_current_membership(row: ScoredClient) -> bool:
    if not row.last_membership_payment_date:
        return False
    try:
        paid_at = datetime.strptime(row.last_membership_payment_date, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (date.today() - paid_at).days <= 31


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
