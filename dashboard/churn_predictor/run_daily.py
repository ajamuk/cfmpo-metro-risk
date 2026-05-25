from __future__ import annotations

import sys
from datetime import datetime

from .aimharder import AimHarderClient, AimHarderError
from .config import load_settings, save_center_tokens
from .local_data import load_signals
from .report import _has_current_membership, write_reports
from .scoring import score_client


def main() -> int:
    settings = load_settings()
    if not settings.centers:
        print("No hay tokens configurados. Crea .env a partir de .env.example.")
        return 2

    signals = load_signals(settings.data_dir)
    scored = []
    errors = []

    for center in settings.centers:
        try:
            clients, fresh_center = AimHarderClient(center).list_clients()
        except AimHarderError as exc:
            errors.append(str(exc))
            continue
        if fresh_center != center:
            save_center_tokens(fresh_center)

        active_clients = [client for client in clients if not client.get("deactivation_date")]
        for client in active_clients:
            client_id = str(client.get("id") or client.get("Id") or "")
            name = " ".join(
                str(client.get(key) or "").strip()
                for key in ("name", "first_surname", "second_surname")
                if str(client.get(key) or "").strip()
            )
            scored.append(
                score_client(
                    client=client,
                    center=fresh_center.name,
                    signal=signals.find(
                        client_id=client_id,
                        email=str(client.get("email") or ""),
                        phone=str(client.get("mobile_number") or client.get("mobile") or ""),
                        name=name,
                    ),
                    high=settings.high_risk_score,
                    medium=settings.medium_risk_score,
                )
            )

    scored.sort(key=lambda row: row.score, reverse=True)
    csv_path, html_path = write_reports(scored, settings.report_dir, settings.min_report_score)

    print(f"Informe generado: {csv_path}")
    print(f"Vista HTML: {html_path}")
    print(f"Clientes evaluados: {len(scored)}")
    listed = sum(1 for row in scored if row.risk != "Baja real" and _has_current_membership(row))
    print(f"Socios con tarifa activa listados: {listed}")
    if errors:
        print("Errores:")
        for error in errors:
            print(f"- {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
