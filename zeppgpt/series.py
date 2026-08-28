"""Decoders for compact series returned by Zepp workout detail responses.

The Zepp workout API uses semicolon-delimited records and comma-delimited
components.  This module only assigns meanings that have been corroborated by
the observed payloads and an independent Zepp workout parser.  Wider records
such as ``lap`` deliberately remain positional until their columns are known.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias

Number: TypeAlias = int | float
NumericRecord: TypeAlias = tuple[Number | None, ...]

COORDINATE_FACTOR = 100_000_000
MISSING_ALTITUDE_CENTIMETERS = -2_000_000


class SeriesDecodeError(ValueError):
    """A field-aware decoding error that never echoes raw payload values."""


@dataclass(frozen=True, slots=True)
class ChangePoint:
    """A new value taking effect at an offset from workout start."""

    offset_seconds: int
    values: tuple[Number, ...]


@dataclass(frozen=True, slots=True)
class ChangeSeries:
    """Sparse run-length/delta encoded values and their encoded time span."""

    points: tuple[ChangePoint, ...]
    span_seconds: int


@dataclass(frozen=True, slots=True)
class RoutePoint:
    """One decoded route sample at an offset from workout start."""

    offset_seconds: int
    latitude_degrees: float | None
    longitude_degrees: float | None
    altitude_meters: float | None
    accuracy_meters: float | None
    course_degrees: float | None
    flag: int | None


def split_records(encoded: str) -> list[tuple[str, ...]]:
    """Split a Zepp series while retaining meaningful empty records.

    A single final empty item is the terminator produced by a trailing
    semicolon.  Empty records inside the series are positional placeholders,
    most notably in route coordinates, and must not be discarded.
    """

    if not isinstance(encoded, str):
        raise TypeError("encoded series must be a string")
    if encoded == "":
        return []

    raw_records = encoded.split(";")
    if raw_records[-1] == "":
        raw_records.pop()
    return [tuple(record.split(",")) for record in raw_records]


def require_record_width(
    records: Sequence[Sequence[Any]],
    *,
    expected_columns: int,
    field: str,
) -> None:
    """Validate positional record width without interpreting its columns."""

    if expected_columns < 1:
        raise ValueError("expected_columns must be positive")
    for index, record in enumerate(records):
        if len(record) != expected_columns:
            raise SeriesDecodeError(
                f"{field} record {index} has {len(record)} columns; "
                f"expected {expected_columns}"
            )


def parse_numeric_records(
    encoded: str,
    *,
    expected_columns: int,
    field: str,
) -> tuple[NumericRecord, ...]:
    """Decode a fixed-width numeric series, preserving missing components."""

    records = split_records(encoded)
    normalized: list[tuple[str, ...]] = []
    for record in records:
        if record == ("",) and expected_columns > 1:
            normalized.append(("",) * expected_columns)
        else:
            normalized.append(record)

    require_record_width(
        normalized,
        expected_columns=expected_columns,
        field=field,
    )
    return tuple(
        tuple(
            None if component == "" else _parse_number(component, field, index)
            for component in record
        )
        for index, record in enumerate(normalized)
    )


def decode_change_series(
    encoded: str,
    *,
    field: str,
    value_columns: int = 1,
    cumulative_columns: frozenset[int] = frozenset(),
    default_delta_seconds: int = 1,
) -> ChangeSeries:
    """Decode Zepp's sparse time-delta/run-length value representation.

    Each record is ``time_delta,value...``.  A blank time delta represents one
    second.  Values in ``cumulative_columns`` are deltas from the previous
    value; all other values are absolute.  The first record spans
    ``time_delta + 1`` seconds and later records span ``time_delta`` seconds,
    matching the Zepp interpolation convention.
    """

    if value_columns < 1:
        raise ValueError("value_columns must be positive")
    if default_delta_seconds < 0:
        raise ValueError("default_delta_seconds cannot be negative")
    if any(column < 0 or column >= value_columns for column in cumulative_columns):
        raise ValueError("cumulative column index is out of range")

    records = parse_numeric_records(
        encoded,
        expected_columns=value_columns + 1,
        field=field,
    )
    current: list[Number] = [0] * value_columns
    points: list[ChangePoint] = []
    cursor = 0

    for record_index, record in enumerate(records):
        delta_raw = record[0]
        delta = (
            default_delta_seconds
            if delta_raw is None
            else _nonnegative_integer(delta_raw, field, record_index)
        )

        for column, value in enumerate(record[1:]):
            if value is None:
                raise SeriesDecodeError(
                    f"{field} record {record_index} is missing value column {column}"
                )
            current[column] = (
                current[column] + value
                if column in cumulative_columns
                else value
            )

        points.append(ChangePoint(cursor, tuple(current)))
        cursor += delta + (1 if record_index == 0 else 0)

    return ChangeSeries(tuple(points), cursor)


def decode_route(detail: Mapping[str, Any]) -> tuple[RoutePoint, ...]:
    """Decode time, delta coordinates, altitude, and GPS metadata.

    Coordinates are latitude then longitude, scaled by 1e8 and delta-encoded.
    Route timestamps are cumulative second offsets.  Missing coordinate records
    retain their position but yield ``None`` for that sample.
    """

    time_encoded = _series_text(detail, "time")
    time_records = parse_numeric_records(
        time_encoded,
        expected_columns=1,
        field="time",
    )
    coordinate_records = parse_numeric_records(
        _series_text(detail, "longitude_latitude"),
        expected_columns=2,
        field="longitude_latitude",
    )
    if coordinate_records and len(coordinate_records) != len(time_records):
        raise SeriesDecodeError(
            "longitude_latitude record count does not match time record count"
        )

    optional_records = {
        field: parse_numeric_records(
            _series_text(detail, field),
            expected_columns=1,
            field=field,
        )
        for field in ("altitude", "accuracy", "course", "flag")
    }

    elapsed = 0
    latitude_scaled = 0
    longitude_scaled = 0
    points: list[RoutePoint] = []
    has_coordinates = bool(coordinate_records)

    for index, time_record in enumerate(time_records):
        time_delta = time_record[0]
        if time_delta is None:
            raise SeriesDecodeError(f"time record {index} is missing its value")
        elapsed += _nonnegative_integer(time_delta, "time", index)

        latitude: float | None = None
        longitude: float | None = None
        if has_coordinates:
            latitude_delta, longitude_delta = coordinate_records[index]
            if latitude_delta is not None and longitude_delta is not None:
                latitude_scaled += _integer(latitude_delta, "longitude_latitude", index)
                longitude_scaled += _integer(
                    longitude_delta, "longitude_latitude", index
                )
                latitude = latitude_scaled / COORDINATE_FACTOR
                longitude = longitude_scaled / COORDINATE_FACTOR
                if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                    raise SeriesDecodeError(
                        f"longitude_latitude record {index} decodes outside valid ranges"
                    )
            elif latitude_delta is not None or longitude_delta is not None:
                raise SeriesDecodeError(
                    f"longitude_latitude record {index} has only one coordinate component"
                )

        altitude_raw = _optional_value(optional_records["altitude"], index)
        altitude = (
            None
            if altitude_raw in (None, MISSING_ALTITUDE_CENTIMETERS)
            else float(altitude_raw) / 100
        )
        accuracy_raw = _optional_value(optional_records["accuracy"], index)
        course_raw = _optional_value(optional_records["course"], index)
        flag_raw = _optional_value(optional_records["flag"], index)

        points.append(
            RoutePoint(
                offset_seconds=elapsed,
                latitude_degrees=latitude,
                longitude_degrees=longitude,
                altitude_meters=altitude,
                accuracy_meters=(
                    None if accuracy_raw is None else float(accuracy_raw)
                ),
                course_degrees=None if course_raw is None else float(course_raw),
                flag=(
                    None
                    if flag_raw is None
                    else _integer(flag_raw, "flag", index)
                ),
            )
        )

    return tuple(points)


def _series_text(detail: Mapping[str, Any], field: str) -> str:
    value = detail.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SeriesDecodeError(f"{field} is not a string series")
    return value


def _optional_value(records: Sequence[NumericRecord], index: int) -> Number | None:
    if index >= len(records):
        return None
    return records[index][0]


def _parse_number(component: str, field: str, record_index: int) -> Number:
    try:
        return int(component)
    except ValueError:
        try:
            value = float(component)
        except ValueError:
            raise SeriesDecodeError(
                f"{field} record {record_index} contains a non-numeric component"
            ) from None
        if not math.isfinite(value):
            raise SeriesDecodeError(
                f"{field} record {record_index} contains a non-finite component"
            )
        return value


def _integer(value: Number, field: str, record_index: int) -> int:
    if isinstance(value, float) and not value.is_integer():
        raise SeriesDecodeError(
            f"{field} record {record_index} requires an integer component"
        )
    return int(value)


def _nonnegative_integer(value: Number, field: str, record_index: int) -> int:
    result = _integer(value, field, record_index)
    if result < 0:
        raise SeriesDecodeError(
            f"{field} record {record_index} has a negative time delta"
        )
    return result
