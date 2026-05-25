from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


ROOT = Path(__file__).resolve().parents[1]
TOKEN_STORE = ROOT / ".aimharder_tokens.json"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class CenterConfig:
    name: str
    base_url: str
    access_token: str
    refresh_token: Optional[str] = None


@dataclass(frozen=True)
class Settings:
    centers: List[CenterConfig]
    report_dir: Path
    data_dir: Path
    min_report_score: int
    high_risk_score: int
    medium_risk_score: int
    lookback_days: int
    dashboard_host: str
    dashboard_port: int
    dashboard_user: str
    dashboard_password: str
    injuries_sheet_url: str
    injuries_sheet_name: str


def _default_centers() -> str:
    return json.dumps(
        [
            {
                "name": "CrossFit MetroPolitano",
                "base_url": "https://api.aimharder.com",
                "access_token_env": "AIMHARDER_ACCESS_TOKEN",
                "refresh_token_env": "AIMHARDER_REFRESH_TOKEN",
            }
        ]
    )


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    raw_centers = os.environ.get("AIMHARDER_CENTERS", _default_centers())
    try:
        center_defs = json.loads(raw_centers)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"AIMHARDER_CENTERS no es JSON valido: {exc}") from exc

    token_overrides = _load_token_store()
    centers: List[CenterConfig] = []
    for item in center_defs:
        override = token_overrides.get(item["name"], {})
        access_token = override.get("access_token") or item.get("access_token") or os.environ.get(item.get("access_token_env", ""))
        refresh_token = override.get("refresh_token") or item.get("refresh_token") or os.environ.get(item.get("refresh_token_env", ""))
        if not access_token:
            continue
        centers.append(
            CenterConfig(
                name=item["name"],
                base_url=item.get("base_url", "https://api.aimharder.com").rstrip("/"),
                access_token=access_token,
                refresh_token=refresh_token or None,
            )
        )

    return Settings(
        centers=centers,
        report_dir=ROOT / os.environ.get("REPORT_DIR", "reports"),
        data_dir=ROOT / os.environ.get("DATA_DIR", "data"),
        min_report_score=int(os.environ.get("MIN_REPORT_SCORE", "31")),
        high_risk_score=int(os.environ.get("HIGH_RISK_SCORE", "61")),
        medium_risk_score=int(os.environ.get("MEDIUM_RISK_SCORE", "31")),
        lookback_days=int(os.environ.get("PAYMENT_LOOKBACK_DAYS", "120")),
        dashboard_host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=int(os.environ.get("DASHBOARD_PORT", "8787")),
        dashboard_user=os.environ.get("DASHBOARD_USER", ""),
        dashboard_password=os.environ.get("DASHBOARD_PASSWORD", ""),
        injuries_sheet_url=os.environ.get("INJURIES_SHEET_URL", ""),
        injuries_sheet_name=os.environ.get("INJURIES_SHEET_NAME", "Las Rosas"),
    )


def save_center_tokens(center: CenterConfig) -> None:
    data = _load_token_store()
    data[center.name] = {
        "access_token": center.access_token,
        "refresh_token": center.refresh_token or "",
    }
    TOKEN_STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_token_store() -> dict:
    if not TOKEN_STORE.exists():
        return {}
    try:
        return json.loads(TOKEN_STORE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
