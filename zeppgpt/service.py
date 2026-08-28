"""Read-only application service joining the Zepp client and normalizers."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from datetime import UTC, date, datetime
from threading import RLock
from typing import Any, Protocol

from .models import (
    SampleSelection,
    SubdivisionSelection,
    WorkoutDetail,
    WorkoutId,
    WorkoutSummary,
)
from .normalizers import (
    normalize_samples,
    normalize_subdivisions,
    normalize_summary,
    normalize_workout,
    select_samples,
    select_subdivisions,
)

MAX_LIST_LIMIT = 100
HISTORY_SCAN_LIMIT = 500
HISTORY_SCAN_PAGES = 10
DETAIL_CACHE_SIZE = 50


class WorkoutApi(Protocol):
    active_host: str | None

    def probe_hosts(self) -> tuple[str, dict[str, Any]]: ...

    def list_workouts(
        self,
        *,
        limit: int,
        max_pages: int,
        start_date: date | None = None,
        end_date: date | None = None,
        first_page: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]: ...

    def get_workout_detail(self, *, track_id: str, source: str) -> dict[str, Any]: ...


class WorkoutServiceError(RuntimeError):
    """Base class for safe service-level failures."""


class WorkoutNotFoundError(WorkoutServiceError):
    """The requested compound workout ID was not in bounded history."""


class ZeppWorkoutService:
    """Small single-account service with bounded in-memory caches."""

    def __init__(self, client: WorkoutApi) -> None:
        self._client = client
        self._summary_items: dict[WorkoutId, dict[str, Any]] = {}
        self._detail_payloads: OrderedDict[WorkoutId, dict[str, Any]] = OrderedDict()
        self._lock = RLock()

    def list_workouts(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        sport_type: str | None = None,
        limit: int = 20,
    ) -> tuple[WorkoutSummary, ...]:
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        if start_date and end_date and start_date > end_date:
            raise ValueError("start_date cannot be after end_date")

        with self._lock:
            summaries = self._refresh_history(
                start_date=start_date,
                end_date=end_date,
            )
            if sport_type and sport_type.strip():
                summaries = [
                    summary
                    for summary in summaries
                    if _matches_sport_type(summary, sport_type)
                ]
            summaries.sort(key=_summary_sort_key, reverse=True)
            return tuple(summaries[:limit])

    def get_workout(self, workout_id: str | WorkoutId) -> WorkoutDetail:
        with self._lock:
            identity = _workout_id(workout_id)
            summary_item = self._find_summary_item(identity)
            detail_payload = self._detail_payload(identity)
            return normalize_workout(summary_item, detail_payload)

    def get_latest_workout(self, sport_type: str | None = None) -> WorkoutDetail:
        matches = self.list_workouts(sport_type=sport_type, limit=1)
        if not matches:
            raise WorkoutNotFoundError("No workouts matched the requested sport type")
        return self.get_workout(matches[0].workout_id)

    def get_workout_samples(
        self,
        workout_id: str | WorkoutId,
        *,
        start_offset_seconds: int | None = None,
        end_offset_seconds: int | None = None,
        require_fields: Iterable[str] = (),
        max_points: int = 200,
    ) -> tuple[SampleSelection, tuple[str, ...]]:
        with self._lock:
            identity = _workout_id(workout_id)
            summary_item = self._find_summary_item(identity)
            summary = normalize_summary(summary_item)
            detail_payload = self._detail_payload(identity)
            workout = normalize_workout(summary_item, detail_payload)
            samples = normalize_samples(
                detail_payload,
                start_time_utc=summary.start_time_utc,
            )
            selection = select_samples(
                samples,
                start_offset_seconds=start_offset_seconds,
                end_offset_seconds=end_offset_seconds,
                require_fields=require_fields,
                max_points=max_points,
            )
            return selection, workout.available_sample_fields

    def get_workout_subdivisions(
        self,
        workout_id: str | WorkoutId,
        *,
        kinds: Iterable[str] = (),
        max_records: int = 200,
    ) -> tuple[SubdivisionSelection, tuple[str, ...]]:
        with self._lock:
            identity = _workout_id(workout_id)
            summary_item = self._find_summary_item(identity)
            summary = normalize_summary(summary_item)
            detail_payload = self._detail_payload(identity)
            workout = normalize_workout(summary_item, detail_payload)
            subdivisions = normalize_subdivisions(
                detail_payload,
                start_time_utc=summary.start_time_utc,
                summary=summary,
            )
            selection = select_subdivisions(
                subdivisions,
                kinds=kinds,
                max_records=max_records,
            )
            return selection, workout.available_subdivision_kinds

    def _refresh_history(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[WorkoutSummary]:
        first_page: dict[str, Any] | None = None
        if self._client.active_host is None:
            _, first_page = self._client.probe_hosts()
        items, _ = self._client.list_workouts(
            limit=HISTORY_SCAN_LIMIT,
            max_pages=HISTORY_SCAN_PAGES,
            start_date=start_date,
            end_date=end_date,
            first_page=first_page,
        )
        result: list[WorkoutSummary] = []
        for item in items:
            summary = normalize_summary(item)
            self._summary_items[summary.workout_id] = dict(item)
            result.append(summary)
        return result

    def _find_summary_item(self, workout_id: WorkoutId) -> dict[str, Any]:
        cached = self._summary_items.get(workout_id)
        if cached is not None:
            return cached
        self._refresh_history()
        cached = self._summary_items.get(workout_id)
        if cached is None:
            raise WorkoutNotFoundError("Workout was not found in bounded Zepp history")
        return cached

    def _detail_payload(self, workout_id: WorkoutId) -> dict[str, Any]:
        cached = self._detail_payloads.get(workout_id)
        if cached is not None:
            self._detail_payloads.move_to_end(workout_id)
            return cached
        payload = self._client.get_workout_detail(
            track_id=workout_id.track_id,
            source=workout_id.source,
        )
        self._detail_payloads[workout_id] = payload
        self._detail_payloads.move_to_end(workout_id)
        while len(self._detail_payloads) > DETAIL_CACHE_SIZE:
            self._detail_payloads.popitem(last=False)
        return payload


def _workout_id(value: str | WorkoutId) -> WorkoutId:
    if isinstance(value, WorkoutId):
        return value
    return WorkoutId.parse(value)


def _matches_sport_type(summary: WorkoutSummary, requested: str) -> bool:
    query = requested.strip().casefold().replace("-", "_").replace(" ", "_")
    workout_type = summary.workout_type
    candidates = {
        workout_type.name.casefold(),
        workout_type.category.casefold(),
    }
    if workout_type.zepp_type_id is not None:
        candidates.update(
            {
                str(workout_type.zepp_type_id),
                f"zepp_{workout_type.zepp_type_id}",
                f"unknown_{workout_type.zepp_type_id}",
            }
        )
    return query in candidates


def _summary_sort_key(summary: WorkoutSummary) -> datetime:
    return summary.start_time_utc or datetime.min.replace(tzinfo=UTC)
