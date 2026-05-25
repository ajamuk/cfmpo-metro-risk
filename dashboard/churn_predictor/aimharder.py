from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from typing import Any, Dict, Iterable, List, Tuple

from .config import CenterConfig


class AimHarderError(RuntimeError):
    pass


class AimHarderClient:
    def __init__(self, center: CenterConfig, pause_seconds: float = 0.35):
        self.center = center
        self.pause_seconds = pause_seconds

    def list_clients(self) -> Tuple[List[Dict[str, Any]], CenterConfig]:
        return self._list_cursor_endpoint("/clients", "clients")

    def list_clients_no_booking_since(self, date_str: str) -> Tuple[List[Dict[str, Any]], CenterConfig]:
        # Endpoint específico de AimHarder para clientes sin reserva desde una fecha.
        # La documentación lo declara paginado por `page`.
        clients: List[Dict[str, Any]] = []
        page = 1
        while page <= 500:
            payload = self._get(f"/clients/no-booking/{date_str}", params={"page": str(page)})
            rows, pagination = self._normalise_list_response(payload, "clients")
            clients.extend(rows)
            has_more = pagination.get("hasMore")
            if has_more is True:
                page += 1
                time.sleep(self.pause_seconds)
                continue
            if len(rows) >= 100:
                page += 1
                time.sleep(self.pause_seconds)
                continue
            break
        return clients, self.center

    def _list_cursor_endpoint(self, path: str, default_key: str) -> Tuple[List[Dict[str, Any]], CenterConfig]:
        rows_out: List[Dict[str, Any]] = []
        cursor = ""

        while True:
            payload = self._get(path, params={"cursor": cursor})
            rows, pagination = self._normalise_list_response(payload, default_key)
            rows_out.extend(rows)

            next_cursor = pagination.get("nextCursor") or pagination.get("next_cursor")
            if next_cursor:
                cursor = str(next_cursor)
            else:
                break
            time.sleep(self.pause_seconds)

        return rows_out, self.center

    def _get(self, path: str, params: Dict[str, str] | None = None, retry_refresh: bool = True) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.center.base_url}{path}{query}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.center.access_token}",
                "Content-Type": "application/json",
                "User-Agent": "crossfit-metropolitano-churn/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 410 and retry_refresh and self.center.refresh_token:
                self.center = self._refresh_tokens()
                return self._get(path, params=params, retry_refresh=False)
            raise AimHarderError(f"{self.center.name}: API {exc.code} en {path}: {body}") from exc
        except urllib.error.URLError as exc:
            raise AimHarderError(f"{self.center.name}: no se pudo conectar con AimHarder: {exc}") from exc

    def _refresh_tokens(self) -> CenterConfig:
        request = urllib.request.Request(
            f"{self.center.base_url}/auth/tokens/refresh",
            method="GET",
            headers={
                "Authorization": f"Bearer {self.center.refresh_token}",
                "Content-Type": "application/json",
                "User-Agent": "crossfit-metropolitano-churn/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        access = payload.get("access-token")
        refresh = payload.get("refresh-token")
        if not access:
            raise AimHarderError(f"{self.center.name}: AimHarder no devolvio nuevo access token")
        return replace(self.center, access_token=access, refresh_token=refresh or self.center.refresh_token)

    @staticmethod
    def _normalise_list_response(payload: Any, default_key: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if isinstance(payload, list):
            return payload, {}
        if not isinstance(payload, dict):
            return [], {}
        data = payload.get("data")
        if data is None:
            data = payload.get(default_key) or payload.get("clients") or []
        if isinstance(data, dict):
            data = [data]
        pagination = payload.get("pagination") or {}
        return list(data or []), pagination
