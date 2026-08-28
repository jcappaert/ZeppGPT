"""Normalize Zepp history and detail payloads into sport-neutral models."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .diagnostics import redact
from .models import (
    SAMPLE_VALUE_FIELDS,
    SampleSelection,
    SubdivisionSelection,
    WorkoutDetail,
    WorkoutId,
    WorkoutSample,
    WorkoutSubdivision,
    WorkoutSummary,
)
from .series import (
    ChangePoint,
    RoutePoint,
    SeriesDecodeError,
    decode_change_series,
    decode_route,
    parse_numeric_records,
    split_records,
)
from .workout_types import resolve_workout_type

MAX_SAMPLE_POINTS = 10_000
MAX_SUBDIVISION_RECORDS = 1_000

_SUMMARY_COMMON_FIELDS = {
    "trackid",
    "source",
    "type",
    "sport_mode",
    "end_time",
    "run_time",
    "pause_time",
    "pauseTimeWithMillis",
    "dis",
    "highPrecisionDistance",
    "calorie",
    "avg_heart_rate",
    "min_heart_rate",
    "max_heart_rate",
    "total_step",
    "elevationGain",
    "elevationLoss",
    "altitude_ascend",
    "altitude_descend",
    "exercise_load",
    "te",
    "anaerobic_te",
    "bind_device",
}

_RUN_WALK_FIELDS = {
    "avg_frequency",
    "max_frequency",
    "avg_stride_length",
    "avg_pace",
    "VO2_max",
}

_SWIMMING_FIELDS = {
    "swim_pool_length",
    "swim_style",
    "swolf",
    "total_strokes",
    "total_trips",
    "number_of_break",
    "avg_distance_per_stroke",
    "avg_stroke_speed",
    "max_stroke_speed",
    "freestyle_length",
    "breast_stroke_length",
    "back_stroke_length",
    "butterfly_length",
    "medley_length",
    "other_stroke_length",
}

_ROWING_FIELDS = {
    "avg_frequency",
    "max_frequency",
    "avg_pace",
    "avg_pulloar_time",
    "avg_return_time",
    "strokes",
    "total_strokes",
}

_SWIM_STROKE_TYPES = {
    # Confirmed by reconciling pool-length records with the summary's
    # stroke-specific distances.
    1: "breaststroke",
    2: "freestyle",
}

_POOL_SET_RECORD_KIND = 5
_POOL_LENGTH_RECORD_KIND = 7
_LAP_COLUMNS = 70
_SPLIT_COLUMNS = 15
_TIMING_TOLERANCE_SECONDS = 2.0
_WALL_DURATION_KINDS = frozenset(
    {"pause", "pool_length", "kilometer_split", "mile_split"}
)

_DECODED_DETAIL_FIELDS = {
    "trackid",
    "source",
    "time",
    "longitude_latitude",
    "altitude",
    "accuracy",
    "course",
    "flag",
    "pace",
    "heart_rate",
    "speed",
    "currentDistance",
    "gait",
    "pause",
    "kilo_pace",
    "mile_pace",
    "lap",
    "strengthSets",
}


class NormalizationError(ValueError):
    """A safe schema error that does not echo raw Zepp values."""


def normalize_summary(item: Mapping[str, Any]) -> WorkoutSummary:
    """Normalize one Zepp history summary and retain unclassified fields."""

    track_id = _required_text(item, "trackid")
    source = _required_text(item, "source")
    workout_id = WorkoutId(track_id=track_id, source=source)
    zepp_type_id = _optional_integer(item.get("type"))
    workout_type = resolve_workout_type(zepp_type_id)

    start_time = _unix_datetime(track_id)
    end_time = _unix_datetime(item.get("end_time"))
    duration = _nonnegative_number(item.get("run_time"))
    if start_time is None and end_time is not None and duration is not None:
        start_time = end_time - timedelta(seconds=duration)

    pause_duration = _nonnegative_number(item.get("pause_time"))
    if pause_duration is None:
        pause_milliseconds = _nonnegative_number(item.get("pauseTimeWithMillis"))
        pause_duration = (
            None if pause_milliseconds is None else pause_milliseconds / 1000
        )

    distance = _nonnegative_number(item.get("highPrecisionDistance"))
    if distance is None:
        distance = _nonnegative_number(item.get("dis"))

    elevation_gain, elevation_gain_source, elevation_gain_warning = (
        _normalized_elevation(
            item.get("elevationGain"),
            item.get("altitude_ascend"),
            distance_meters=distance,
        )
    )
    elevation_loss, elevation_loss_source, elevation_loss_warning = (
        _normalized_elevation(
            item.get("elevationLoss"),
            item.get("altitude_descend"),
            distance_meters=distance,
        )
    )

    sport_metrics: dict[str, Any] = {}
    consumed_sport_fields: set[str] = set()
    if workout_type.category in {"running", "walking", "hiking"}:
        consumed_sport_fields.update(_RUN_WALK_FIELDS)
        _put_positive(
            sport_metrics,
            "average_step_frequency_per_minute",
            item.get("avg_frequency"),
        )
        _put_positive(
            sport_metrics,
            "maximum_step_frequency_per_minute",
            item.get("max_frequency"),
        )
        stride_centimeters = _positive_number(item.get("avg_stride_length"))
        if stride_centimeters is not None:
            sport_metrics["average_stride_length_meters"] = (
                stride_centimeters / 100
            )
        _put_positive(
            sport_metrics,
            "vo2_max_milliliters_per_kilogram_per_minute",
            item.get("VO2_max"),
        )
    elif workout_type.category == "swimming":
        consumed_sport_fields.update(_SWIMMING_FIELDS)
        _put_positive(
            sport_metrics,
            "pool_length_meters",
            item.get("swim_pool_length"),
        )
        _put_nonnegative_integer(
            sport_metrics, "swim_style_id", item.get("swim_style")
        )
        _put_positive(sport_metrics, "swolf", item.get("swolf"))
        _put_positive_integer(
            sport_metrics, "total_strokes", item.get("total_strokes")
        )
        _put_positive_integer(
            sport_metrics, "pool_lengths", item.get("total_trips")
        )
        _put_nonnegative_integer(
            sport_metrics, "break_count", item.get("number_of_break")
        )
        _put_positive(
            sport_metrics,
            "average_distance_per_stroke_meters",
            item.get("avg_distance_per_stroke"),
        )
        average_stroke_rate = _positive_number(item.get("avg_stroke_speed"))
        if average_stroke_rate is not None:
            sport_metrics["average_stroke_rate_per_minute"] = (
                average_stroke_rate * 60
            )
        maximum_stroke_rate = _positive_number(item.get("max_stroke_speed"))
        if maximum_stroke_rate is not None:
            sport_metrics["maximum_stroke_rate_per_minute"] = (
                maximum_stroke_rate * 60
            )
        for raw_name, normalized_name in (
            ("freestyle_length", "freestyle_distance_meters"),
            ("breast_stroke_length", "breaststroke_distance_meters"),
            ("back_stroke_length", "backstroke_distance_meters"),
            ("butterfly_length", "butterfly_distance_meters"),
            ("medley_length", "medley_distance_meters"),
            ("other_stroke_length", "other_stroke_distance_meters"),
        ):
            _put_positive(sport_metrics, normalized_name, item.get(raw_name))
    elif workout_type.category == "rowing":
        consumed_sport_fields.update(_ROWING_FIELDS)
        strokes = _positive_integer(item.get("total_strokes"))
        if strokes is None:
            strokes = _positive_integer(item.get("strokes"))
        if strokes is not None:
            sport_metrics["stroke_count"] = strokes
        _put_positive(
            sport_metrics,
            "average_stroke_rate_per_minute",
            item.get("avg_frequency"),
        )
        _put_positive(
            sport_metrics,
            "maximum_stroke_rate_per_minute",
            item.get("max_frequency"),
        )
        _put_positive(
            sport_metrics,
            "average_pull_time_seconds",
            item.get("avg_pulloar_time"),
        )
        _put_positive(
            sport_metrics,
            "average_return_time_seconds",
            item.get("avg_return_time"),
        )

    if workout_type.category in {"running", "walking", "hiking", "rowing"}:
        average_pace = _positive_number(item.get("avg_pace"))
        pace_source = "zepp"
        if average_pace is None and duration and distance:
            average_pace = duration / distance
            pace_source = "derived"
        if average_pace is not None:
            sport_metrics["average_pace_seconds_per_meter"] = average_pace
            sport_metrics["average_speed_meters_per_second"] = 1 / average_pace

    consumed = _SUMMARY_COMMON_FIELDS | consumed_sport_fields
    raw_metadata = _raw_metadata(item, excluding=consumed)
    raw_elevation = {
        key: item.get(key)
        for key in (
            "elevationGain",
            "elevationLoss",
            "altitude_ascend",
            "altitude_descend",
        )
        if item.get(key) not in (None, "")
    }
    if raw_elevation:
        raw_metadata["elevation_raw"] = raw_elevation
    elevation_warnings = tuple(
        warning
        for warning in (elevation_gain_warning, elevation_loss_warning)
        if warning is not None
    )
    if elevation_warnings:
        raw_metadata["normalization_warnings"] = list(elevation_warnings)
    device_name = item.get("bind_device")
    energy_kilocalories = _nonnegative_number(item.get("calorie"))
    provenance = {
        "workout_type": "decoded",
        "start_time_utc": "decoded",
        "duration_seconds": "zepp",
        "distance_meters": "zepp",
        "energy_kilojoules": "decoded",
    }
    if elevation_gain is not None:
        provenance["elevation_gain_meters"] = elevation_gain_source
    if elevation_loss is not None:
        provenance["elevation_loss_meters"] = elevation_loss_source
    if "average_pace_seconds_per_meter" in sport_metrics:
        provenance["sport_metrics.average_pace_seconds_per_meter"] = pace_source
        provenance["sport_metrics.average_speed_meters_per_second"] = "derived"

    return WorkoutSummary(
        workout_id=workout_id,
        workout_type=workout_type,
        sport_mode_id=_optional_integer(item.get("sport_mode")),
        start_time_utc=start_time,
        end_time_utc=end_time,
        duration_seconds=duration,
        pause_duration_seconds=pause_duration,
        distance_meters=distance,
        energy_kilojoules=(
            None if energy_kilocalories is None else energy_kilocalories * 4.184
        ),
        average_heart_rate_bpm=_positive_integer(item.get("avg_heart_rate")),
        minimum_heart_rate_bpm=_positive_integer(item.get("min_heart_rate")),
        maximum_heart_rate_bpm=_positive_integer(item.get("max_heart_rate")),
        total_steps=_positive_integer(item.get("total_step")),
        elevation_gain_meters=elevation_gain,
        elevation_loss_meters=elevation_loss,
        training_load=_positive_integer(item.get("exercise_load")),
        aerobic_effect=_scaled_training_effect(item.get("te")),
        anaerobic_effect=_scaled_training_effect(item.get("anaerobic_te")),
        device_name=(
            str(device_name) if device_name not in (None, "") else None
        ),
        sport_metrics=sport_metrics,
        raw_metadata=raw_metadata,
        provenance=provenance,
    )


def normalize_workout(
    summary_item: Mapping[str, Any],
    detail_payload: Mapping[str, Any],
) -> WorkoutDetail:
    """Normalize summary plus detail metadata, excluding bulk series."""

    summary = normalize_summary(summary_item)
    detail = _detail_data(detail_payload)
    _validate_detail_identity(summary.workout_id, detail)
    summary, elevation_warnings = _validate_elevation_against_altitudes(
        summary, detail
    )

    available_sample_fields = _available_sample_fields(detail)
    available_subdivision_kinds = _available_subdivision_kinds(
        detail, summary=summary
    )
    raw_metadata = _raw_metadata(detail, excluding=_DECODED_DETAIL_FIELDS)
    if elevation_warnings:
        raw_metadata["normalization_warnings"] = list(elevation_warnings)
    return WorkoutDetail(
        summary=summary,
        available_sample_fields=available_sample_fields,
        available_subdivision_kinds=available_subdivision_kinds,
        raw_metadata=raw_metadata,
        provenance={"summary": "decoded"},
    )


def normalize_samples(
    detail_payload: Mapping[str, Any],
    *,
    start_time_utc: datetime | None = None,
) -> tuple[WorkoutSample, ...]:
    """Decode route and sparse measurements into time-keyed change samples."""

    detail = _detail_data(detail_payload)
    if start_time_utc is None:
        start_time_utc = _unix_datetime(detail.get("trackid"))

    try:
        route = decode_route(detail) if _series_present(detail, "time") else ()
        heart_rate = _change_points(
            detail,
            "heart_rate",
            cumulative_columns=frozenset({0}),
        )
        speed = _change_points(detail, "speed")
        # Unlike Zepp's older `distance` field, observed `currentDistance`
        # values are absolute centimetres, not cumulative metre deltas.
        distance = _change_points(detail, "currentDistance")
        gait = _change_points(
            detail,
            "gait",
            value_columns=3,
            cumulative_columns=frozenset({0}),
        )
        pace = _route_pace(detail, route)
    except SeriesDecodeError as exc:
        raise NormalizationError(str(exc)) from None

    route_groups: dict[int, list[tuple[RoutePoint, float | None]]] = defaultdict(list)
    for index, point in enumerate(route):
        route_groups[point.offset_seconds].append((point, pace[index]))

    events = {
        "heart_rate": _point_map(heart_rate),
        "speed": _point_map(speed),
        "distance": _point_map(distance),
        "gait": _point_map(gait),
    }
    offsets = set(route_groups)
    for values in events.values():
        offsets.update(values)
    if not offsets:
        return ()

    current: dict[str, tuple[int | float, ...] | None] = {
        name: None for name in events
    }
    result: list[WorkoutSample] = []
    for offset in sorted(offsets):
        for name, values in events.items():
            if offset in values:
                current[name] = values[offset]

        points = route_groups.get(offset) or [(None, None)]
        for route_point, pace_value in points:
            heart_values = current["heart_rate"]
            speed_values = current["speed"]
            distance_values = current["distance"]
            gait_values = current["gait"]
            timestamp = (
                None
                if start_time_utc is None
                else start_time_utc + timedelta(seconds=offset)
            )
            result.append(
                WorkoutSample(
                    offset_seconds=offset,
                    timestamp_utc=timestamp,
                    latitude_degrees=(
                        None
                        if route_point is None
                        else route_point.latitude_degrees
                    ),
                    longitude_degrees=(
                        None
                        if route_point is None
                        else route_point.longitude_degrees
                    ),
                    altitude_meters=(
                        None if route_point is None else route_point.altitude_meters
                    ),
                    accuracy_meters=(
                        None if route_point is None else route_point.accuracy_meters
                    ),
                    course_degrees=(
                        None if route_point is None else route_point.course_degrees
                    ),
                    gps_flag=None if route_point is None else route_point.flag,
                    heart_rate_bpm=(
                        None
                        if heart_values is None
                        else _exact_integer(heart_values[0], "heart_rate")
                    ),
                    speed_meters_per_second=(
                        None if speed_values is None else float(speed_values[0])
                    ),
                    distance_meters=(
                        None
                        if distance_values is None
                        else float(distance_values[0]) / 100
                    ),
                    pace_seconds_per_meter=pace_value,
                    cumulative_steps=(
                        None
                        if gait_values is None
                        else _exact_integer(gait_values[0], "gait")
                    ),
                    stride_length_meters=(
                        None
                        if gait_values is None
                        else float(gait_values[1]) / 100
                    ),
                    step_frequency_per_minute=(
                        None if gait_values is None else float(gait_values[2])
                    ),
                )
            )
    return tuple(result)


def select_samples(
    samples: Sequence[WorkoutSample],
    *,
    start_offset_seconds: int | None = None,
    end_offset_seconds: int | None = None,
    require_fields: Iterable[str] = (),
    max_points: int = 1_000,
) -> SampleSelection:
    """Filter samples and deterministically downsample to a hard bound."""

    if not 1 <= max_points <= MAX_SAMPLE_POINTS:
        raise ValueError(f"max_points must be between 1 and {MAX_SAMPLE_POINTS}")
    if start_offset_seconds is not None and start_offset_seconds < 0:
        raise ValueError("start_offset_seconds cannot be negative")
    if end_offset_seconds is not None and end_offset_seconds < 0:
        raise ValueError("end_offset_seconds cannot be negative")
    if (
        start_offset_seconds is not None
        and end_offset_seconds is not None
        and start_offset_seconds > end_offset_seconds
    ):
        raise ValueError("start_offset_seconds cannot exceed end_offset_seconds")

    required = frozenset(require_fields)
    if required - SAMPLE_VALUE_FIELDS:
        raise ValueError("unknown required sample field")

    matched = [
        sample
        for sample in samples
        if (
            start_offset_seconds is None
            or sample.offset_seconds >= start_offset_seconds
        )
        and (end_offset_seconds is None or sample.offset_seconds <= end_offset_seconds)
        and (
            not required
            or any(getattr(sample, field) is not None for field in required)
        )
    ]
    selected = _evenly_downsample(matched, max_points)
    return SampleSelection(
        samples=tuple(selected),
        total_samples=len(samples),
        matched_samples=len(matched),
        downsampled=len(selected) < len(matched),
    )


def normalize_subdivisions(
    detail_payload: Mapping[str, Any],
    *,
    start_time_utc: datetime | None = None,
    summary: WorkoutSummary | None = None,
) -> tuple[WorkoutSubdivision, ...]:
    """Decode pauses and dispatch positional records by workout sport."""

    detail = _detail_data(detail_payload)
    if summary is not None and start_time_utc is None:
        start_time_utc = summary.start_time_utc
    if start_time_utc is None:
        start_time_utc = _unix_datetime(detail.get("trackid"))
    result: list[WorkoutSubdivision] = []

    try:
        pauses = parse_numeric_records(
            _series_text(detail, "pause"),
            expected_columns=5,
            field="pause",
        )
    except SeriesDecodeError as exc:
        raise NormalizationError(str(exc)) from None

    for index, record in enumerate(pauses):
        if any(value is None for value in record):
            raise NormalizationError(f"pause record {index} has missing components")
        pause_start = _unix_datetime(record[0])
        pause_type_id = _exact_integer(record[4], "pause")
        start_offset = None
        if pause_start is not None and start_time_utc is not None:
            start_offset = (pause_start - start_time_utc).total_seconds()
        duration = float(record[1])
        result.append(
            WorkoutSubdivision(
                kind="pause",
                index=index,
                start_offset_seconds=start_offset,
                end_offset_seconds=(
                    None if start_offset is None else start_offset + duration
                ),
                start_time_utc=pause_start,
                duration_seconds=duration,
                metrics={
                    "start_sample_index": _exact_integer(record[2], "pause"),
                    "end_sample_index": _exact_integer(record[3], "pause"),
                    "pause_type_id": pause_type_id,
                    "pause_type": {2: "manual", 3: "automatic"}.get(
                        pause_type_id, "unknown"
                    ),
                },
                provenance={
                    "start_offset_seconds": "decoded",
                    "end_offset_seconds": "derived",
                    "duration_seconds": "zepp",
                    "metrics": "decoded",
                },
            )
        )

    result.extend(
        _decode_distance_splits(
            _series_text(detail, "kilo_pace"),
            field="kilo_pace",
            kind="kilometer_split",
            split_distance_meters=1_000,
            start_time_utc=start_time_utc,
            summary=summary,
        )
    )
    result.extend(
        _decode_distance_splits(
            _series_text(detail, "mile_pace"),
            field="mile_pace",
            kind="mile_split",
            split_distance_meters=1_609.344,
            start_time_utc=start_time_utc,
            summary=summary,
        )
    )

    lap_encoded = _series_text(detail, "lap")
    if lap_encoded:
        category = None if summary is None else summary.workout_type.category
        rows = split_records(lap_encoded)
        pool_shaped = any(
            _record_integer(row, 18) in {
                _POOL_SET_RECORD_KIND,
                _POOL_LENGTH_RECORD_KIND,
            }
            for row in rows
            if len(row) == _LAP_COLUMNS
        )
        if category == "swimming" or category is None and pool_shaped:
            result.extend(
                _decode_pool_laps(
                    rows,
                    start_time_utc=start_time_utc,
                    summary=summary,
                )
            )
        elif category == "rowing":
            result.extend(
                _decode_rowing_laps(
                    rows,
                    start_time_utc=start_time_utc,
                    summary=summary,
                )
            )
        else:
            result.extend(_raw_subdivisions(rows, kind="lap_record"))

    strength_sets = _series_text(detail, "strengthSets")
    if strength_sets:
        try:
            decoded_sets = json.loads(strength_sets)
        except json.JSONDecodeError:
            result.append(
                WorkoutSubdivision(
                    kind="strength_sets_raw",
                    index=0,
                    raw_metadata={"encoded": strength_sets},
                    provenance={"raw_metadata": "zepp"},
                )
            )
        else:
            if isinstance(decoded_sets, list):
                for index, value in enumerate(decoded_sets):
                    result.append(
                        WorkoutSubdivision(
                            kind="strength_set",
                            index=index,
                            raw_metadata={"value": value},
                            provenance={"raw_metadata": "zepp"},
                        )
                    )
            elif decoded_sets not in (None, {}):
                result.append(
                    WorkoutSubdivision(
                        kind="strength_sets_raw",
                        index=0,
                        raw_metadata={"value": decoded_sets},
                        provenance={"raw_metadata": "zepp"},
                    )
                )

    return tuple(
        _enforce_subdivision_timing(
            result,
            summary=summary,
            start_time_utc=start_time_utc,
        )
    )


def _decode_distance_splits(
    encoded: str,
    *,
    field: str,
    kind: str,
    split_distance_meters: float,
    start_time_utc: datetime | None,
    summary: WorkoutSummary | None,
) -> tuple[WorkoutSubdivision, ...]:
    """Decode the verified subset of Zepp's 15-column distance split row."""

    if not encoded:
        return ()
    result: list[WorkoutSubdivision] = []
    for fallback_index, record in enumerate(split_records(encoded)):
        if len(record) != _SPLIT_COLUMNS:
            result.append(
                WorkoutSubdivision(
                    kind=f"{kind}_raw",
                    index=fallback_index,
                    raw_columns=record,
                    raw_metadata={
                        "column_count": len(record),
                        "warning": (
                            f"{field} schema drift: expected {_SPLIT_COLUMNS} columns"
                        ),
                    },
                )
            )
            continue

        index = _record_integer(record, 0)
        if index is None:
            index = fallback_index
        duration_milliseconds = _record_positive(record, 6)
        duration = (
            duration_milliseconds / 1_000
            if duration_milliseconds is not None
            else _record_positive(record, 1)
        )
        end_offset = _record_nonnegative(record, 5)
        start_offset = _start_offset(end_offset, duration)
        average_heart_rate = _record_positive_integer(record, 4)
        energy_kilocalories = _record_nonnegative(record, 7)
        frequency = _record_positive(record, 13)

        metrics: dict[str, Any] = {
            "distance_meters": split_distance_meters,
        }
        provenance = {
            "duration_seconds": "decoded",
            "start_offset_seconds": "derived",
            "end_offset_seconds": "decoded",
            "distance_meters": "decoded",
        }
        if duration is not None:
            metrics["pace_seconds_per_kilometer"] = (
                duration * 1_000 / split_distance_meters
            )
            metrics["speed_meters_per_second"] = (
                split_distance_meters / duration
            )
            provenance["pace_seconds_per_kilometer"] = "derived"
            provenance["speed_meters_per_second"] = "derived"
        if kind == "mile_split" and duration is not None:
            metrics["pace_seconds_per_mile"] = duration
            provenance["pace_seconds_per_mile"] = "decoded"
        if average_heart_rate is not None:
            metrics["average_heart_rate_bpm"] = average_heart_rate
            provenance["average_heart_rate_bpm"] = "decoded"
        if energy_kilocalories is not None:
            metrics["energy_kilojoules"] = energy_kilocalories * 4.184
            provenance["energy_kilojoules"] = "decoded"
        if frequency is not None:
            if summary is not None and summary.workout_type.category == "rowing":
                metrics["stroke_rate_per_minute"] = frequency
                provenance["stroke_rate_per_minute"] = "decoded"
            else:
                metrics["cadence_per_minute"] = frequency
                provenance["cadence_per_minute"] = "decoded"

        result.append(
            WorkoutSubdivision(
                kind=kind,
                index=index,
                start_offset_seconds=start_offset,
                end_offset_seconds=end_offset,
                start_time_utc=_offset_time(start_time_utc, start_offset),
                duration_seconds=duration,
                metrics=metrics,
                raw_columns=record,
                raw_metadata={"column_count": len(record)},
                provenance=provenance,
            )
        )
    return tuple(result)


def _decode_pool_laps(
    rows: Sequence[Sequence[str]],
    *,
    start_time_utc: datetime | None,
    summary: WorkoutSummary | None,
) -> tuple[WorkoutSubdivision, ...]:
    """Decode verified fields from Zepp pool-length and pool-set rows."""

    result: list[WorkoutSubdivision] = []
    for fallback_index, record in enumerate(rows):
        if len(record) != _LAP_COLUMNS:
            result.extend(_raw_subdivisions((record,), kind="pool_record_raw"))
            continue
        record_kind_id = _record_integer(record, 18)
        kind = {
            _POOL_SET_RECORD_KIND: "pool_set",
            _POOL_LENGTH_RECORD_KIND: "pool_length",
        }.get(record_kind_id, "pool_record")
        index = _record_integer(record, 0)
        if index is None:
            index = fallback_index
        duration = _lap_duration(record)
        end_offset = _lap_end_offset(record)
        start_offset = _start_offset(end_offset, duration)
        distance = _record_positive(record, 2)
        pace = _record_positive(record, 9)
        strokes = _record_positive_integer(record, 13)
        stroke_type_id = _record_nonnegative_integer(record, 26)
        stroke_rate_per_second = _record_positive(record, 12)
        energy_kilocalories = _record_nonnegative(record, 15)

        metrics: dict[str, Any] = {"zepp_record_kind_id": record_kind_id}
        provenance: dict[str, str] = {
            "duration_seconds": "decoded",
            "start_offset_seconds": "derived",
            "end_offset_seconds": "decoded",
            "zepp_record_kind_id": "decoded",
        }
        _metric(metrics, provenance, "distance_meters", distance, "decoded")
        _metric(
            metrics,
            provenance,
            "average_heart_rate_bpm",
            _record_positive_integer(record, 4),
            "decoded",
        )
        _metric(
            metrics,
            provenance,
            "maximum_heart_rate_bpm",
            _record_positive_integer(record, 67),
            "decoded",
        )
        _metric(metrics, provenance, "stroke_type_id", stroke_type_id, "decoded")
        if stroke_type_id is not None:
            metrics["stroke_type"] = _SWIM_STROKE_TYPES.get(
                stroke_type_id, "unknown"
            )
            provenance["stroke_type"] = "decoded"
        _metric(metrics, provenance, "stroke_count", strokes, "decoded")
        if stroke_rate_per_second is not None:
            metrics["stroke_rate_per_minute"] = stroke_rate_per_second * 60
            provenance["stroke_rate_per_minute"] = "decoded"
        _metric(
            metrics,
            provenance,
            "swolf",
            _record_positive_integer(record, 14),
            "decoded",
        )
        if pace is not None:
            metrics["pace_seconds_per_100_meters"] = pace * 100
            metrics["speed_meters_per_second"] = 1 / pace
            provenance["pace_seconds_per_100_meters"] = "decoded"
            provenance["speed_meters_per_second"] = "derived"
        if distance is not None and strokes is not None and strokes > 0:
            metrics["distance_per_stroke_meters"] = distance / strokes
            provenance["distance_per_stroke_meters"] = "derived"
        _metric(
            metrics,
            provenance,
            "cumulative_average_distance_per_stroke_meters",
            _record_positive(record, 53),
            "decoded",
        )
        if energy_kilocalories is not None:
            metrics["energy_kilojoules"] = energy_kilocalories * 4.184
            provenance["energy_kilojoules"] = "decoded"

        result.append(
            WorkoutSubdivision(
                kind=kind,
                index=index,
                start_offset_seconds=start_offset,
                end_offset_seconds=end_offset,
                start_time_utc=_offset_time(start_time_utc, start_offset),
                duration_seconds=duration,
                metrics=metrics,
                raw_columns=tuple(record),
                raw_metadata={"column_count": len(record)},
                provenance=provenance,
            )
        )
    return tuple(
        _reconcile_pool_set_timing(
            result,
            start_time_utc=start_time_utc,
            summary=summary,
        )
    )


def _decode_rowing_laps(
    rows: Sequence[Sequence[str]],
    *,
    start_time_utc: datetime | None,
    summary: WorkoutSummary | None,
) -> tuple[WorkoutSubdivision, ...]:
    """Decode the corroborated subset of rowing-machine lap rows."""

    result: list[WorkoutSubdivision] = []
    for fallback_index, record in enumerate(rows):
        if len(record) != _LAP_COLUMNS:
            result.extend(_raw_subdivisions((record,), kind="rowing_interval_raw"))
            continue
        index = _record_integer(record, 0)
        if index is None:
            index = fallback_index
        duration = _lap_duration(record)
        # Columns 5/57 increase, but aggregate rowing rows use an
        # internal clock domain: their values exceed the summary wall clock
        # and do not reconcile with active duration plus recorded pauses.
        # Preserve the verified active duration and metrics, but do not emit
        # fabricated wall-clock boundaries.
        start_offset = None
        end_offset = None
        metrics: dict[str, Any] = {}
        provenance: dict[str, str] = {
            "duration_seconds": "decoded",
        }
        distance = _record_positive(record, 2)
        if distance is None and len(rows) == 1 and summary is not None:
            distance = summary.distance_meters
            distance_source = "zepp"
        else:
            distance_source = "decoded"
        _metric(metrics, provenance, "distance_meters", distance, distance_source)
        _metric(
            metrics,
            provenance,
            "average_heart_rate_bpm",
            _record_positive_integer(record, 4),
            "decoded",
        )
        _metric(
            metrics,
            provenance,
            "stroke_count",
            _record_positive_integer(record, 13),
            "decoded",
        )
        _metric(
            metrics,
            provenance,
            "stroke_rate_per_minute",
            _record_positive(record, 16),
            "decoded",
        )
        energy_kilocalories = _record_nonnegative(record, 15)
        if energy_kilocalories is not None:
            metrics["energy_kilojoules"] = energy_kilocalories * 4.184
            provenance["energy_kilojoules"] = "decoded"
        result.append(
            WorkoutSubdivision(
                kind="rowing_interval",
                index=index,
                start_offset_seconds=start_offset,
                end_offset_seconds=end_offset,
                start_time_utc=_offset_time(start_time_utc, start_offset),
                duration_seconds=duration,
                metrics=metrics,
                raw_columns=tuple(record),
                raw_metadata={
                    "column_count": len(record),
                    "normalization_warnings": [
                        "Aggregate rowing timing was omitted because its raw clock domain is not a verified wall-clock offset"
                    ],
                },
                provenance=provenance,
            )
        )
    return tuple(result)


def _raw_subdivisions(
    rows: Sequence[Sequence[str]], *, kind: str
) -> tuple[WorkoutSubdivision, ...]:
    return tuple(
        WorkoutSubdivision(
            kind=kind,
            index=index,
            raw_columns=tuple(record),
            raw_metadata={"column_count": len(record)},
            provenance={"raw_columns": "zepp"},
        )
        for index, record in enumerate(rows)
    )


def _metric(
    metrics: dict[str, Any],
    provenance: dict[str, str],
    key: str,
    value: Any,
    source: str,
) -> None:
    if value is not None:
        metrics[key] = value
        provenance[key] = source


def _record_nonnegative(record: Sequence[str], column: int) -> float | None:
    if column >= len(record):
        return None
    return _nonnegative_number(record[column])


def _record_positive(record: Sequence[str], column: int) -> float | None:
    if column >= len(record):
        return None
    return _positive_number(record[column])


def _record_integer(record: Sequence[str], column: int) -> int | None:
    if column >= len(record):
        return None
    return _optional_integer(record[column])


def _record_nonnegative_integer(
    record: Sequence[str], column: int
) -> int | None:
    value = _record_integer(record, column)
    return value if value is not None and value >= 0 else None


def _record_positive_integer(record: Sequence[str], column: int) -> int | None:
    value = _record_integer(record, column)
    return value if value is not None and value > 0 else None


def _lap_duration(record: Sequence[str]) -> float | None:
    milliseconds = _record_positive(record, 43)
    return (
        milliseconds / 1_000
        if milliseconds is not None
        else _record_positive(record, 1)
    )


def _lap_end_offset(record: Sequence[str]) -> float | None:
    milliseconds = _record_nonnegative(record, 57)
    return (
        milliseconds / 1_000
        if milliseconds is not None
        else _record_nonnegative(record, 5)
    )


def _start_offset(
    end_offset_seconds: float | None, duration_seconds: float | None
) -> float | None:
    if end_offset_seconds is None or duration_seconds is None:
        return None
    return max(0.0, end_offset_seconds - duration_seconds)


def _wall_clock_duration(summary: WorkoutSummary | None) -> float | None:
    if (
        summary is None
        or summary.start_time_utc is None
        or summary.end_time_utc is None
    ):
        return None
    duration = (summary.end_time_utc - summary.start_time_utc).total_seconds()
    return duration if duration >= 0 else None


def _reconcile_pool_set_timing(
    subdivisions: Sequence[WorkoutSubdivision],
    *,
    start_time_utc: datetime | None,
    summary: WorkoutSummary | None,
) -> list[WorkoutSubdivision]:
    """Derive set boundaries from explicitly ordered aggregate/member rows.

    Zepp emits the set records before the length records. Set distance (and,
    when present, stroke count) lets us partition the ordered length records
    without trusting the aggregate timing columns. Those timing columns are
    demonstrably in a different clock domain on some workouts.
    """

    lengths = [item for item in subdivisions if item.kind == "pool_length"]
    cursor = 0
    reconciled: list[WorkoutSubdivision] = []
    for item in subdivisions:
        if item.kind != "pool_set":
            reconciled.append(item)
            continue

        target_distance = _positive_number(item.metrics.get("distance_meters"))
        members: list[WorkoutSubdivision] = []
        member_distance = 0.0
        if target_distance is not None:
            while cursor < len(lengths) and member_distance < target_distance - 0.01:
                member = lengths[cursor]
                distance = _positive_number(member.metrics.get("distance_meters"))
                if distance is None:
                    break
                members.append(member)
                member_distance += distance
                cursor += 1

        distance_matches = (
            target_distance is not None
            and members
            and abs(member_distance - target_distance) <= 0.01
        )
        expected_strokes = _positive_integer(item.metrics.get("stroke_count"))
        member_strokes = [
            _positive_integer(member.metrics.get("stroke_count"))
            for member in members
        ]
        strokes_match = (
            expected_strokes is None
            or any(value is None for value in member_strokes)
            or sum(value for value in member_strokes if value is not None)
            == expected_strokes
        )
        starts = [
            member.start_offset_seconds
            for member in members
            if member.start_offset_seconds is not None
        ]
        ends = [
            member.end_offset_seconds
            for member in members
            if member.end_offset_seconds is not None
        ]
        timing_matches = (
            distance_matches
            and strokes_match
            and len(starts) == len(members)
            and len(ends) == len(members)
        )

        provenance = dict(item.provenance)
        raw_metadata = dict(item.raw_metadata)
        warnings = list(raw_metadata.get("normalization_warnings", ()))
        if timing_matches:
            start_offset = min(starts)
            end_offset = max(ends)
            provenance["start_offset_seconds"] = "derived"
            provenance["end_offset_seconds"] = "derived"
            raw_metadata["timing_basis"] = (
                "ordered member lengths reconciled by aggregate distance and stroke count"
            )
        else:
            start_offset = None
            end_offset = None
            provenance.pop("start_offset_seconds", None)
            provenance.pop("end_offset_seconds", None)
            warnings.append(
                "Pool-set timing was omitted because member lengths could not be reconciled"
            )
        if warnings:
            raw_metadata["normalization_warnings"] = warnings

        reconciled.append(
            replace(
                item,
                start_offset_seconds=start_offset,
                end_offset_seconds=end_offset,
                start_time_utc=_offset_time(start_time_utc, start_offset),
                raw_metadata=raw_metadata,
                provenance=provenance,
            )
        )
    return reconciled


def _enforce_subdivision_timing(
    subdivisions: Sequence[WorkoutSubdivision],
    *,
    summary: WorkoutSummary | None,
    start_time_utc: datetime | None,
) -> list[WorkoutSubdivision]:
    """Bound normalized offsets to the workout's summary wall clock."""

    wall_duration = _wall_clock_duration(summary)
    validated: list[WorkoutSubdivision] = []
    for item in subdivisions:
        start = item.start_offset_seconds
        end = item.end_offset_seconds
        duration = item.duration_seconds
        provenance = dict(item.provenance)
        raw_metadata = dict(item.raw_metadata)
        warnings = list(raw_metadata.get("normalization_warnings", ()))

        negative_duration = duration is not None and duration < 0
        invalid = (
            negative_duration
            or start is not None
            and start < 0
            or end is not None
            and end < 0
            or start is not None
            and end is not None
            and end < start
        )
        if invalid:
            start = None
            end = None
            if negative_duration:
                duration = None
                provenance.pop("duration_seconds", None)
            provenance.pop("start_offset_seconds", None)
            provenance.pop("end_offset_seconds", None)
            warnings.append("Subdivision timing failed basic ordering invariants")

        if end is not None and wall_duration is not None and end > wall_duration:
            if end <= wall_duration + _TIMING_TOLERANCE_SECONDS:
                end = wall_duration
                provenance["end_offset_seconds"] = "derived"
                if item.kind in _WALL_DURATION_KINDS and duration is not None:
                    start = max(0.0, end - duration)
                    provenance["start_offset_seconds"] = "derived"
                warnings.append(
                    "Subdivision end offset was bounded to the workout wall clock"
                )
            else:
                start = None
                end = None
                provenance.pop("start_offset_seconds", None)
                provenance.pop("end_offset_seconds", None)
                warnings.append(
                    "Subdivision timing was omitted because it exceeded the workout wall clock"
                )

        if (
            item.kind in _WALL_DURATION_KINDS
            and start is not None
            and end is not None
            and duration is not None
            and abs((end - start) - duration) > _TIMING_TOLERANCE_SECONDS
        ):
            start = max(0.0, end - duration)
            provenance["start_offset_seconds"] = "derived"

        if warnings:
            raw_metadata["normalization_warnings"] = list(dict.fromkeys(warnings))
        validated.append(
            replace(
                item,
                start_offset_seconds=start,
                end_offset_seconds=end,
                start_time_utc=_offset_time(start_time_utc, start),
                duration_seconds=duration,
                raw_metadata=raw_metadata,
                provenance=provenance,
            )
        )
    return validated


def _offset_time(
    start_time_utc: datetime | None, offset_seconds: float | None
) -> datetime | None:
    if start_time_utc is None or offset_seconds is None:
        return None
    return start_time_utc + timedelta(seconds=offset_seconds)


def select_subdivisions(
    subdivisions: Sequence[WorkoutSubdivision],
    *,
    kinds: Iterable[str] = (),
    max_records: int = 200,
) -> SubdivisionSelection:
    """Filter subdivisions and cap the result independently from samples."""

    if not 1 <= max_records <= MAX_SUBDIVISION_RECORDS:
        raise ValueError(
            f"max_records must be between 1 and {MAX_SUBDIVISION_RECORDS}"
        )
    selected_kinds = frozenset(kinds)
    if any(not kind.strip() for kind in selected_kinds):
        raise ValueError("subdivision kind cannot be empty")
    matched = [
        subdivision
        for subdivision in subdivisions
        if not selected_kinds or subdivision.kind in selected_kinds
    ]
    returned = matched[:max_records]
    return SubdivisionSelection(
        subdivisions=tuple(returned),
        total_subdivisions=len(subdivisions),
        matched_subdivisions=len(matched),
        truncated=len(returned) < len(matched),
    )


def _detail_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    if data is None:
        return payload
    if not isinstance(data, Mapping):
        raise NormalizationError("detail data is not an object")
    return data


def _raw_metadata(
    values: Mapping[str, Any], *, excluding: set[str]
) -> dict[str, Any]:
    unclassified = {key: value for key, value in values.items() if key not in excluding}
    sanitized = redact(unclassified)
    if not isinstance(sanitized, dict):
        raise TypeError("raw metadata redaction did not return an object")
    return sanitized


def _validate_detail_identity(workout_id: WorkoutId, detail: Mapping[str, Any]) -> None:
    detail_track = detail.get("trackid")
    if detail_track not in (None, "") and str(detail_track) != workout_id.track_id:
        raise NormalizationError("detail track ID does not match its summary")
    detail_source = detail.get("source")
    if detail_source not in (None, "") and str(detail_source) != workout_id.source:
        raise NormalizationError("detail source does not match its summary")


def _available_sample_fields(detail: Mapping[str, Any]) -> tuple[str, ...]:
    fields: set[str] = set()
    if _series_present(detail, "longitude_latitude"):
        fields.update({"latitude_degrees", "longitude_degrees"})
    mapping = {
        "altitude": {"altitude_meters"},
        "accuracy": {"accuracy_meters"},
        "course": {"course_degrees"},
        "flag": {"gps_flag"},
        "heart_rate": {"heart_rate_bpm"},
        "speed": {"speed_meters_per_second"},
        "currentDistance": {"distance_meters"},
        "pace": {"pace_seconds_per_meter"},
        "gait": {
            "cumulative_steps",
            "stride_length_meters",
            "step_frequency_per_minute",
        },
    }
    for field, normalized_fields in mapping.items():
        if _series_present(detail, field):
            fields.update(normalized_fields)
    return tuple(sorted(fields))


def _available_subdivision_kinds(
    detail: Mapping[str, Any], *, summary: WorkoutSummary | None = None
) -> tuple[str, ...]:
    kinds: list[str] = []
    for field, kind in (
        ("pause", "pause"),
        ("kilo_pace", "kilometer_split"),
        ("mile_pace", "mile_split"),
    ):
        if _series_present(detail, field):
            kinds.append(kind)
    if _series_present(detail, "lap"):
        rows = split_records(_series_text(detail, "lap"))
        category = None if summary is None else summary.workout_type.category
        record_kind_ids = {
            _record_integer(record, 18)
            for record in rows
            if len(record) == _LAP_COLUMNS
        }
        if category == "swimming" or category is None and record_kind_ids & {
            _POOL_SET_RECORD_KIND,
            _POOL_LENGTH_RECORD_KIND,
        }:
            if _POOL_SET_RECORD_KIND in record_kind_ids:
                kinds.append("pool_set")
            if _POOL_LENGTH_RECORD_KIND in record_kind_ids:
                kinds.append("pool_length")
            if not record_kind_ids & {
                _POOL_SET_RECORD_KIND,
                _POOL_LENGTH_RECORD_KIND,
            }:
                kinds.append("pool_record")
        elif category == "rowing":
            kinds.append("rowing_interval")
        else:
            kinds.append("lap_record")
    strength = detail.get("strengthSets")
    if isinstance(strength, str) and strength not in ("", "[]"):
        kinds.append("strength_set")
    return tuple(kinds)


def _change_points(
    detail: Mapping[str, Any],
    field: str,
    *,
    value_columns: int = 1,
    cumulative_columns: frozenset[int] = frozenset(),
) -> tuple[ChangePoint, ...]:
    encoded = _series_text(detail, field)
    if not encoded:
        return ()
    return decode_change_series(
        encoded,
        field=field,
        value_columns=value_columns,
        cumulative_columns=cumulative_columns,
    ).points


def _route_pace(
    detail: Mapping[str, Any], route: Sequence[RoutePoint]
) -> tuple[float | None, ...]:
    encoded = _series_text(detail, "pace")
    if not route:
        return ()
    if not encoded:
        return (None,) * len(route)
    records = parse_numeric_records(encoded, expected_columns=1, field="pace")
    if len(records) != len(route):
        raise SeriesDecodeError("pace record count does not match time record count")
    return tuple(None if record[0] is None else float(record[0]) for record in records)


def _point_map(points: Sequence[ChangePoint]) -> dict[int, tuple[int | float, ...]]:
    return {point.offset_seconds: point.values for point in points}


def _evenly_downsample(
    samples: Sequence[WorkoutSample], max_points: int
) -> list[WorkoutSample]:
    if len(samples) <= max_points:
        return list(samples)
    if max_points == 1:
        return [samples[0]]
    last = len(samples) - 1
    indices = [round(index * last / (max_points - 1)) for index in range(max_points)]
    return [samples[index] for index in indices]


def _series_present(detail: Mapping[str, Any], field: str) -> bool:
    value = detail.get(field)
    return isinstance(value, str) and bool(value)


def _series_text(detail: Mapping[str, Any], field: str) -> str:
    value = detail.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise NormalizationError(f"{field} is not a string series")
    return value


def _normalized_elevation(
    centimetres_value: Any,
    metres_fallback_value: Any,
    *,
    distance_meters: float | None,
) -> tuple[float | None, str, str | None]:
    """Normalize Zepp's centimetre total and cross-check its metre twin.

    Cross-field validation and independent implementations of the same API
    show that the higher-precision total is one hundred times its integer-metre
    twin. The centimetre field is therefore divided by 100; the integer-metre
    field remains a fallback and cross-check.
    """

    centimetres = _positive_number(centimetres_value)
    metres_fallback = _positive_number(metres_fallback_value)
    if centimetres is not None:
        metres = centimetres / 100
        if metres_fallback is not None and abs(metres - metres_fallback) > max(
            5.0, metres_fallback * 0.2
        ):
            return (
                metres_fallback,
                "zepp",
                (
                    "Zepp centimetre elevation total disagreed with its metre "
                    "fallback; the fallback was used"
                ),
            )
        hard_limit = (
            20_000.0
            if distance_meters is None
            else max(2_000.0, distance_meters * 1.5)
        )
        if metres > hard_limit:
            if metres_fallback is not None and metres_fallback <= hard_limit:
                return (
                    metres_fallback,
                    "zepp",
                    (
                        "Zepp centimetre elevation total failed a physical sanity "
                        "check; the metre fallback was used"
                    ),
                )
            return (
                None,
                "decoded",
                "Zepp elevation total failed a physical sanity check and was omitted",
            )
        return round(metres, 2), "decoded", None
    if metres_fallback is not None:
        return metres_fallback, "zepp", None
    return None, "zepp", None


def _validate_elevation_against_altitudes(
    summary: WorkoutSummary, detail: Mapping[str, Any]
) -> tuple[WorkoutSummary, tuple[str, ...]]:
    """Reject elevation totals that are irreconcilable with route altitudes."""

    if not _series_present(detail, "time") or not _series_present(
        detail, "altitude"
    ):
        return summary, ()
    try:
        route = decode_route(detail)
    except SeriesDecodeError:
        return summary, (
            "Elevation validation skipped because the altitude stream did not decode",
        )
    altitudes = [
        point.altitude_meters
        for point in route
        if point.altitude_meters is not None
        and math.isfinite(point.altitude_meters)
        and -500 <= point.altitude_meters <= 10_000
    ]
    if len(altitudes) < 2:
        return summary, ()

    altitude_span = max(altitudes) - min(altitudes)
    distance_limit = (
        0.0
        if summary.distance_meters is None
        else summary.distance_meters * 0.5
    )
    maximum_plausible = max(500.0, altitude_span * 50, distance_limit)
    warnings: list[str] = []
    gain = summary.elevation_gain_meters
    loss = summary.elevation_loss_meters
    provenance = dict(summary.provenance)
    if gain is not None and gain > maximum_plausible:
        gain = None
        provenance.pop("elevation_gain_meters", None)
        warnings.append(
            "Elevation gain was omitted because it failed validation against the "
            "decoded altitude range"
        )
    if loss is not None and loss > maximum_plausible:
        loss = None
        provenance.pop("elevation_loss_meters", None)
        warnings.append(
            "Elevation loss was omitted because it failed validation against the "
            "decoded altitude range"
        )
    if not warnings:
        return summary, ()
    return (
        replace(
            summary,
            elevation_gain_meters=gain,
            elevation_loss_meters=loss,
            provenance=provenance,
        ),
        tuple(warnings),
    )


def _required_text(item: Mapping[str, Any], field: str) -> str:
    value = item.get(field)
    if value in (None, ""):
        raise NormalizationError(f"summary {field} is missing")
    return str(value)


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_number(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _positive_number(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _optional_integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _positive_integer(value: Any) -> int | None:
    number = _optional_integer(value)
    return number if number is not None and number > 0 else None


def _unix_datetime(value: Any) -> datetime | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _exact_integer(value: float | None, field: str) -> int:
    if value is None or isinstance(value, float) and not value.is_integer():
        raise NormalizationError(f"{field} requires an integer value")
    return int(value)


def _put_positive(target: dict[str, Any], key: str, value: Any) -> None:
    number = _positive_number(value)
    if number is not None:
        target[key] = number


def _put_positive_integer(target: dict[str, Any], key: str, value: Any) -> None:
    number = _positive_integer(value)
    if number is not None:
        target[key] = number


def _put_nonnegative_integer(target: dict[str, Any], key: str, value: Any) -> None:
    number = _optional_integer(value)
    if number is not None and number >= 0:
        target[key] = number


def _scaled_training_effect(value: Any) -> float | None:
    number = _positive_number(value)
    return None if number is None else number / 10
