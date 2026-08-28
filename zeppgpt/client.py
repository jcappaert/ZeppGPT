"""Minimal read-only client for reverse-engineered Zepp workout endpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import ZeppConfig

HISTORY_PATH = "/v1/sport/run/history.json"
DETAIL_PATH = "/v1/sport/run/detail.json"


class ZeppApiError(RuntimeError):
    """A safe diagnostic error that never includes credentials."""

    def __init__(
        self,
        message: str,
        *,
        host: str | None = None,
        path: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.host = host
        self.path = path
        self.status = status


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    payload: Any


class Transport(Protocol):
    def get_json(
        self,
        *,
        host: str,
        path: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> ApiResponse: ...


class UrllibTransport:
    """Small stdlib JSON transport with controlled, secret-free errors."""

    def get_json(
        self,
        *,
        host: str,
        path: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> ApiResponse:
        url = f"{host}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(url, method="GET", headers=dict(headers))
        try:
            with urlopen(request, timeout=timeout) as response:
                status = response.status
                body = response.read()
        except HTTPError as exc:
            status = exc.code
            try:
                body = exc.read()
            except OSError:
                body = b""
            message = _http_error_message(status)
            raise ZeppApiError(message, host=host, path=path, status=status) from None
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            reason_name = type(reason or exc).__name__
            raise ZeppApiError(
                f"Network request failed ({reason_name})",
                host=host,
                path=path,
            ) from None

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ZeppApiError(
                "Zepp returned a non-JSON response",
                host=host,
                path=path,
                status=status,
            ) from None
        return ApiResponse(status=status, payload=payload)


class ZeppApiClient:
    def __init__(
        self,
        config: ZeppConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibTransport()
        self.active_host: str | None = None

    def probe_hosts(self) -> tuple[str, dict[str, Any]]:
        """Validate the configured token by fetching the first history page."""
        failures: list[tuple[str, ZeppApiError]] = []
        for host in self.config.api_hosts:
            try:
                payload = self._request_history_page(host=host, stop_track_id=None)
            except ZeppApiError as exc:
                failures.append((host, exc))
                continue
            self.active_host = host
            return host, payload

        if failures:
            status_summary = ", ".join(
                f"{host}={error.status or 'network/schema error'}"
                for host, error in failures
            )
            if any(error.status == 401 for _, error in failures):
                message = "Zepp rejected the app token; configure a fresh token"
            else:
                message = f"No configured Zepp API host succeeded ({status_summary})"
            raise ZeppApiError(message)
        raise ZeppApiError("No Zepp API hosts are configured")

    def list_workouts(
        self,
        *,
        limit: int = 20,
        max_pages: int = 3,
        start_date: date | None = None,
        end_date: date | None = None,
        first_page: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if not 1 <= max_pages <= 50:
            raise ValueError("max_pages must be between 1 and 50")
        if start_date and end_date and start_date > end_date:
            raise ValueError("start_date cannot be after end_date")

        host = self._require_host()
        pages: list[dict[str, Any]] = []
        workouts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        stop_track_id: str | None = None

        for page_number in range(max_pages):
            payload = (
                first_page
                if page_number == 0 and first_page is not None
                else self._request_history_page(host=host, stop_track_id=stop_track_id)
            )
            pages.append(payload)
            data = _response_data(payload, host=host, path=HISTORY_PATH)
            summary = data.get("summary", [])
            if not isinstance(summary, list):
                raise ZeppApiError(
                    "Unexpected Zepp history schema: data.summary is not a list",
                    host=host,
                    path=HISTORY_PATH,
                )

            new_items = 0
            for item in summary:
                if not isinstance(item, dict):
                    continue
                track_id = str(item.get("trackid", ""))
                source = str(item.get("source") or self.config.source)
                signature = (track_id, source)
                if not track_id or signature in seen:
                    continue
                seen.add(signature)
                new_items += 1
                workout_date = workout_start_date(item)
                if start_date and workout_date and workout_date < start_date:
                    continue
                if end_date and workout_date and workout_date > end_date:
                    continue
                workouts.append(item)
                if len(workouts) >= limit:
                    return workouts, pages

            next_id = data.get("next")
            if next_id in (None, "", 0, -1, "0", "-1") or new_items == 0:
                break
            next_id_text = str(next_id)
            if next_id_text == stop_track_id:
                break
            stop_track_id = next_id_text

        return workouts, pages

    def get_workout_detail(self, *, track_id: str, source: str) -> dict[str, Any]:
        if not track_id.strip():
            raise ValueError("track_id cannot be empty")
        if not source.strip():
            raise ValueError("source cannot be empty")
        host = self._require_host()
        response = self.transport.get_json(
            host=host,
            path=DETAIL_PATH,
            params={"trackid": track_id, "source": source},
            headers=self._headers(),
            timeout=self.config.timeout_seconds,
        )
        payload = response.payload
        _response_data(payload, host=host, path=DETAIL_PATH)
        if not isinstance(payload, dict):
            raise ZeppApiError(
                "Unexpected Zepp detail schema",
                host=host,
                path=DETAIL_PATH,
                status=response.status,
            )
        return payload

    def _request_history_page(
        self, *, host: str, stop_track_id: str | None
    ) -> dict[str, Any]:
        params = {
            "source": self.config.source,
            "userid": self.config.user_id,
            "needSubData": "1",
        }
        if stop_track_id:
            params["stopTrackId"] = stop_track_id
        response = self.transport.get_json(
            host=host,
            path=HISTORY_PATH,
            params=params,
            headers=self._headers(),
            timeout=self.config.timeout_seconds,
        )
        payload = response.payload
        _response_data(payload, host=host, path=HISTORY_PATH)
        if not isinstance(payload, dict):
            raise ZeppApiError(
                "Unexpected Zepp history schema",
                host=host,
                path=HISTORY_PATH,
                status=response.status,
            )
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": "ZeppGPT/0.1 (read-only)",
            "apptoken": self.config.app_token,
            "appname": "com.huami.midong",
            "appPlatform": "android_phone",
        }

    def _require_host(self) -> str:
        if self.active_host:
            return self.active_host
        host, _ = self.probe_hosts()
        return host


def workout_start_date(item: Mapping[str, Any]) -> date | None:
    # Zepp uses a Unix-seconds workout start as trackid.  This remains correct
    # for paused workouts where end_time - active run_time is too late.
    try:
        track_timestamp = int(float(item.get("trackid", 0)))
    except (TypeError, ValueError):
        track_timestamp = 0
    if track_timestamp > 0:
        try:
            return datetime.fromtimestamp(track_timestamp, tz=UTC).date()
        except (OverflowError, OSError, ValueError):
            pass

    try:
        end_time = int(float(item.get("end_time", 0)))
        duration = int(float(item.get("run_time", 0)))
    except (TypeError, ValueError):
        return None
    if end_time <= 0:
        return None
    try:
        return datetime.fromtimestamp(end_time - max(duration, 0), tz=UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def compact_workout(item: Mapping[str, Any]) -> dict[str, Any]:
    start_date = workout_start_date(item)
    return {
        "track_id": str(item.get("trackid", "")),
        "source": item.get("source"),
        "date_utc": start_date.isoformat() if start_date else None,
        "type_id": item.get("type"),
        "sport_mode": item.get("sport_mode"),
        "duration_seconds": _number(item.get("run_time")),
        "distance_meters": _number(item.get("dis")),
        "calories": _number(item.get("calorie")),
        "average_heart_rate": _number(item.get("avg_heart_rate")),
        "maximum_heart_rate": _number(item.get("max_heart_rate")),
        "device": item.get("bind_device") or None,
    }


def _response_data(payload: Any, *, host: str, path: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ZeppApiError("Zepp response is not an object", host=host, path=path)
    code = payload.get("code")
    if code not in (None, 1, "1"):
        # Do not echo an upstream message. Although Zepp normally returns a
        # harmless status string, a diagnostic must never risk reflecting a
        # credential or other request data supplied by the remote service.
        safe_code = str(code)[:32]
        raise ZeppApiError(
            f"Zepp API rejected the request (code {safe_code})", host=host, path=path
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ZeppApiError(
            "Unexpected Zepp response: data is not an object", host=host, path=path
        )
    return data


def _number(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _http_error_message(status: int) -> str:
    if status == 401:
        return "Zepp authentication failed (HTTP 401); the app token may be expired"
    if status == 403:
        return "Zepp authorization failed (HTTP 403)"
    if status == 404:
        return "Zepp endpoint was not found on this regional host (HTTP 404)"
    if status == 429:
        return "Zepp rate limited the request (HTTP 429); retry later"
    return f"Zepp request failed (HTTP {status})"
