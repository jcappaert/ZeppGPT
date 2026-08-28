"""Development-only reports for validating workout normalization."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .diagnostics import field_inventory, redact
from .models import SAMPLE_VALUE_FIELDS, WorkoutSample, WorkoutSubdivision
from .normalizers import (
    normalize_samples,
    normalize_subdivisions,
    normalize_summary,
    normalize_workout,
)
from .series import split_records

_PRIVATE_SAMPLE_FIELDS = {"latitude_degrees", "longitude_degrees"}

_LAP_CANDIDATES = {
    0: "index",
    1: "rounded_duration_seconds",
    2: "distance_meters",
    4: "average_heart_rate_bpm",
    5: "rounded_end_offset_seconds",
    9: "pace_seconds_per_meter",
    12: "stroke_rate_per_second",
    13: "stroke_count",
    14: "swolf",
    15: "energy_kilocalories",
    18: "record_kind_id",
    26: "stroke_type_id",
    43: "duration_milliseconds",
    53: "cumulative_average_distance_per_stroke_meters",
    57: "record_completion_milliseconds_length_clock_only",
    67: "maximum_heart_rate_bpm",
}

_SPLIT_CANDIDATES = {
    0: "index",
    1: "rounded_duration_seconds",
    4: "average_heart_rate_bpm",
    5: "end_offset_seconds",
    6: "duration_milliseconds",
    7: "energy_kilocalories",
    13: "cadence_or_stroke_rate_per_minute",
}


def build_workout_inspection(
    summary_item: Mapping[str, Any],
    detail_payload: Mapping[str, Any],
    *,
    sample_limit: int = 5,
    subdivision_limit: int = 200,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Build a bounded, JSON-serializable normalization inspection report."""

    if not 0 <= sample_limit <= 50:
        raise ValueError("sample_limit must be between 0 and 50")
    if not 1 <= subdivision_limit <= 1_000:
        raise ValueError("subdivision_limit must be between 1 and 1000")

    summary = normalize_summary(summary_item)
    workout = normalize_workout(summary_item, detail_payload)
    samples = normalize_samples(
        detail_payload, start_time_utc=summary.start_time_utc
    )
    subdivisions = normalize_subdivisions(detail_payload, summary=summary)
    detail = detail_payload.get("data", detail_payload)
    if not isinstance(detail, Mapping):
        detail = {}

    report: dict[str, Any] = {
        "normalized_summary": workout.summary.to_dict(
            include_raw_metadata=include_raw
        ),
        "workout_wall_clock_duration_seconds": _wall_clock_duration(
            summary_item
        ),
        "raw_summary_field_inventory": field_inventory(dict(summary_item)),
        "raw_detail_field_inventory": field_inventory(dict(detail)),
        "available_sample_streams": list(workout.available_sample_fields),
        "sample_stream_stats": _sample_stats(samples),
        "sample_preview": _sample_preview(samples, sample_limit),
        "available_subdivision_kinds": list(
            workout.available_subdivision_kinds
        ),
        "normalized_subdivisions": [
            subdivision.to_dict(include_raw_metadata=include_raw)
            for subdivision in subdivisions[:subdivision_limit]
        ],
        "subdivisions_truncated": len(subdivisions) > subdivision_limit,
        "column_analysis": {
            field: _column_analysis(
                str(detail.get(field) or ""),
                candidates=(
                    _LAP_CANDIDATES
                    if field == "lap"
                    else _SPLIT_CANDIDATES
                ),
                include_examples=include_raw,
            )
            for field in ("lap", "kilo_pace", "mile_pace")
            if detail.get(field)
        },
        "warnings": _validation_warnings(
            workout.raw_metadata, summary_item, subdivisions
        ),
    }
    if include_raw:
        report["raw_summary"] = redact(dict(summary_item))
    return report


def _sample_stats(samples: Sequence[WorkoutSample]) -> dict[str, Any]:
    stats: dict[str, Any] = {"sample_count": len(samples)}
    if samples:
        stats["offset_range_seconds"] = [
            samples[0].offset_seconds,
            samples[-1].offset_seconds,
        ]
    fields: dict[str, Any] = {}
    for name in sorted(SAMPLE_VALUE_FIELDS):
        values = [getattr(sample, name) for sample in samples]
        present = [value for value in values if value is not None]
        if not present:
            continue
        field_stats: dict[str, Any] = {"present_count": len(present)}
        if name not in _PRIVATE_SAMPLE_FIELDS:
            field_stats.update({"minimum": min(present), "maximum": max(present)})
        fields[name] = field_stats
    stats["fields"] = fields
    return stats


def _sample_preview(
    samples: Sequence[WorkoutSample], limit: int
) -> list[dict[str, Any]]:
    fields = frozenset(SAMPLE_VALUE_FIELDS - _PRIVATE_SAMPLE_FIELDS)
    return [sample.to_dict(fields=fields) for sample in samples[:limit]]


def _column_analysis(
    encoded: str,
    *,
    candidates: Mapping[int, str],
    include_examples: bool,
) -> dict[str, Any]:
    records = split_records(encoded)
    widths = Counter(len(record) for record in records)
    maximum_width = max(widths, default=0)
    columns: list[dict[str, Any]] = []
    for index in range(maximum_width):
        values = [record[index] for record in records if index < len(record)]
        populated = [value for value in values if value != ""]
        numeric: list[float] = []
        numeric_count = 0
        integer_count = 0
        for value in populated:
            try:
                number = float(value)
            except ValueError:
                continue
            if not math.isfinite(number):
                continue
            numeric_count += 1
            integer_count += number.is_integer()
            numeric.append(number)
        if populated and numeric_count == len(populated):
            value_type = "integer" if integer_count == numeric_count else "number"
        elif numeric_count:
            value_type = "mixed"
        else:
            value_type = "string"
        column: dict[str, Any] = {
            "index": index,
            "populated": len(populated),
            "unique": len(set(populated)),
            "type": value_type,
        }
        if numeric:
            column["minimum"] = min(numeric)
            column["maximum"] = max(numeric)
        if index in candidates:
            column["candidate_interpretation"] = candidates[index]
        if include_examples:
            column["examples"] = list(dict.fromkeys(populated))[:3]
        columns.append(column)
    return {
        "record_count": len(records),
        "record_widths": dict(sorted(widths.items())),
        "columns": columns,
    }


def _validation_warnings(
    workout_raw_metadata: Mapping[str, Any],
    summary_item: Mapping[str, Any],
    subdivisions: Sequence[WorkoutSubdivision],
) -> list[str]:
    warnings = list(workout_raw_metadata.get("normalization_warnings", ()))
    wall_duration = _wall_clock_duration(summary_item)
    if wall_duration is not None:
        for item in subdivisions:
            if (
                item.end_offset_seconds is not None
                and item.end_offset_seconds > wall_duration + 0.001
            ):
                warnings.append(
                    f"{item.kind} {item.index} exceeds the workout wall clock"
                )
    for item in subdivisions:
        warnings.extend(item.raw_metadata.get("normalization_warnings", ()))
    lengths = [item for item in subdivisions if item.kind == "pool_length"]
    sets = [item for item in subdivisions if item.kind == "pool_set"]
    if lengths:
        decoded_distance = sum(
            float(item.metrics.get("distance_meters") or 0) for item in lengths
        )
        workout_distance = _float(summary_item.get("highPrecisionDistance"))
        if workout_distance is None:
            workout_distance = _float(summary_item.get("dis"))
        if workout_distance is not None and abs(decoded_distance - workout_distance) > 1:
            warnings.append(
                "Pool-length distances do not reconcile with the workout distance"
            )
        expected_lengths = _integer(summary_item.get("total_trips"))
        if expected_lengths is not None and len(lengths) != expected_lengths:
            warnings.append(
                "Decoded pool-length count does not reconcile with total_trips"
            )
    if sets and any(item.metrics.get("distance_meters") is None for item in sets):
        warnings.append("At least one pool set has no decoded distance")
    return list(dict.fromkeys(warnings))


def _wall_clock_duration(summary_item: Mapping[str, Any]) -> float | None:
    start = _float(summary_item.get("trackid"))
    end = _float(summary_item.get("end_time"))
    if start is None or end is None or end < start:
        return None
    return end - start


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)
