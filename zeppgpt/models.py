"""Sport-neutral domain models for normalized Zepp workouts."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SAMPLE_VALUE_FIELDS = frozenset(
    {
        "latitude_degrees",
        "longitude_degrees",
        "altitude_meters",
        "accuracy_meters",
        "course_degrees",
        "gps_flag",
        "heart_rate_bpm",
        "speed_meters_per_second",
        "distance_meters",
        "pace_seconds_per_meter",
        "cumulative_steps",
        "stride_length_meters",
        "step_frequency_per_minute",
    }
)


@dataclass(frozen=True, slots=True)
class WorkoutId:
    """The compound identity required by Zepp's detail endpoint."""

    track_id: str
    source: str

    def __post_init__(self) -> None:
        if not self.track_id.strip():
            raise ValueError("track_id cannot be empty")
        if not self.source.strip():
            raise ValueError("source cannot be empty")

    @property
    def encoded(self) -> str:
        payload = json.dumps(
            [self.track_id, self.source],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return f"zepp:{token}"

    @classmethod
    def parse(cls, value: str) -> WorkoutId:
        if not isinstance(value, str) or not value.startswith("zepp:"):
            raise ValueError("invalid Zepp workout ID")
        token = value.removeprefix("zepp:")
        if not token or len(token) > 4096:
            raise ValueError("invalid Zepp workout ID")
        try:
            padding = "=" * (-len(token) % 4)
            decoded = base64.urlsafe_b64decode(token + padding).decode("utf-8")
            parts = json.loads(decoded)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid Zepp workout ID") from None
        if (
            not isinstance(parts, list)
            or len(parts) != 2
            or not all(isinstance(part, str) for part in parts)
        ):
            raise ValueError("invalid Zepp workout ID")
        return cls(track_id=parts[0], source=parts[1])

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.encoded,
            "track_id": self.track_id,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class WorkoutType:
    zepp_type_id: int | None
    name: str
    category: str
    known: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "zepp_type_id": self.zepp_type_id,
            "name": self.name,
            "category": self.category,
            "known": self.known,
        }


@dataclass(frozen=True, slots=True)
class WorkoutSummary:
    workout_id: WorkoutId
    workout_type: WorkoutType
    sport_mode_id: int | None
    start_time_utc: datetime | None
    end_time_utc: datetime | None
    duration_seconds: float | None
    pause_duration_seconds: float | None
    distance_meters: float | None
    energy_kilojoules: float | None
    average_heart_rate_bpm: int | None
    minimum_heart_rate_bpm: int | None
    maximum_heart_rate_bpm: int | None
    total_steps: int | None
    elevation_gain_meters: float | None
    elevation_loss_meters: float | None
    training_load: int | None
    aerobic_effect: float | None
    anaerobic_effect: float | None
    device_name: str | None
    sport_metrics: dict[str, Any] = field(default_factory=dict)
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)

    def to_dict(self, *, include_raw_metadata: bool = True) -> dict[str, Any]:
        result = {
            "workout_id": self.workout_id.to_dict(),
            "workout_type": self.workout_type.to_dict(),
            "sport_mode_id": self.sport_mode_id,
            "start_time_utc": _iso_utc(self.start_time_utc),
            "end_time_utc": _iso_utc(self.end_time_utc),
            "duration_seconds": _present_number(
                "duration_seconds", self.duration_seconds
            ),
            "pause_duration_seconds": _present_number(
                "pause_duration_seconds", self.pause_duration_seconds
            ),
            "distance_meters": _present_number(
                "distance_meters", self.distance_meters
            ),
            "energy_kilojoules": _present_number(
                "energy_kilojoules", self.energy_kilojoules
            ),
            "average_heart_rate_bpm": self.average_heart_rate_bpm,
            "minimum_heart_rate_bpm": self.minimum_heart_rate_bpm,
            "maximum_heart_rate_bpm": self.maximum_heart_rate_bpm,
            "total_steps": self.total_steps,
            "elevation_gain_meters": _present_number(
                "elevation_gain_meters", self.elevation_gain_meters
            ),
            "elevation_loss_meters": _present_number(
                "elevation_loss_meters", self.elevation_loss_meters
            ),
            "training_load": self.training_load,
            "aerobic_effect": _present_number(
                "aerobic_effect", self.aerobic_effect
            ),
            "anaerobic_effect": _present_number(
                "anaerobic_effect", self.anaerobic_effect
            ),
            "device_name": self.device_name,
            "sport_metrics": _present_metrics(self.sport_metrics),
            "provenance": dict(self.provenance),
        }
        if include_raw_metadata:
            result["raw_metadata"] = dict(self.raw_metadata)
        return result


@dataclass(frozen=True, slots=True)
class WorkoutDetail:
    """Normalized workout metadata without bulk samples or subdivisions."""

    summary: WorkoutSummary
    available_sample_fields: tuple[str, ...]
    available_subdivision_kinds: tuple[str, ...]
    sport_metrics: dict[str, Any] = field(default_factory=dict)
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)

    @property
    def workout_id(self) -> WorkoutId:
        return self.summary.workout_id

    def to_dict(self, *, include_raw_metadata: bool = True) -> dict[str, Any]:
        result = {
            "summary": self.summary.to_dict(
                include_raw_metadata=include_raw_metadata
            ),
            "available_sample_fields": list(self.available_sample_fields),
            "available_subdivision_kinds": list(
                self.available_subdivision_kinds
            ),
            "sport_metrics": _present_metrics(self.sport_metrics),
            "provenance": dict(self.provenance),
        }
        if include_raw_metadata:
            result["raw_metadata"] = dict(self.raw_metadata)
        return result


@dataclass(frozen=True, slots=True)
class WorkoutSample:
    offset_seconds: int
    timestamp_utc: datetime | None = None
    latitude_degrees: float | None = None
    longitude_degrees: float | None = None
    altitude_meters: float | None = None
    accuracy_meters: float | None = None
    course_degrees: float | None = None
    gps_flag: int | None = None
    heart_rate_bpm: int | None = None
    speed_meters_per_second: float | None = None
    distance_meters: float | None = None
    pace_seconds_per_meter: float | None = None
    cumulative_steps: int | None = None
    stride_length_meters: float | None = None
    step_frequency_per_minute: float | None = None

    def to_dict(self, *, fields: frozenset[str] | None = None) -> dict[str, Any]:
        selected = SAMPLE_VALUE_FIELDS if fields is None else fields
        unknown = selected - SAMPLE_VALUE_FIELDS
        if unknown:
            raise ValueError("unknown workout sample field")
        result: dict[str, Any] = {
            "offset_seconds": self.offset_seconds,
            "timestamp_utc": _iso_utc(self.timestamp_utc),
        }
        for name in sorted(selected):
            value = getattr(self, name)
            result[name] = (
                value
                if name in {"latitude_degrees", "longitude_degrees"}
                else _present_number(name, value)
            )
        return result


@dataclass(frozen=True, slots=True)
class SampleSelection:
    samples: tuple[WorkoutSample, ...]
    total_samples: int
    matched_samples: int
    downsampled: bool

    def to_dict(self, *, fields: frozenset[str] | None = None) -> dict[str, Any]:
        return {
            "total_samples": self.total_samples,
            "matched_samples": self.matched_samples,
            "returned_samples": len(self.samples),
            "downsampled": self.downsampled,
            "samples": [sample.to_dict(fields=fields) for sample in self.samples],
        }


@dataclass(frozen=True, slots=True)
class WorkoutSubdivision:
    kind: str
    index: int
    start_offset_seconds: float | None = None
    end_offset_seconds: float | None = None
    start_time_utc: datetime | None = None
    duration_seconds: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    raw_columns: tuple[str, ...] = ()
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)

    def to_dict(self, *, include_raw_metadata: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "index": self.index,
            "start_offset_seconds": _present_number(
                "start_offset_seconds", self.start_offset_seconds
            ),
            "end_offset_seconds": _present_number(
                "end_offset_seconds", self.end_offset_seconds
            ),
            "start_time_utc": _iso_utc(self.start_time_utc),
            "duration_seconds": _present_number(
                "duration_seconds", self.duration_seconds
            ),
            "metrics": _present_metrics(self.metrics),
            "provenance": dict(self.provenance),
        }
        if include_raw_metadata:
            raw_metadata = dict(self.raw_metadata)
            if self.raw_columns:
                raw_metadata["raw_columns"] = list(self.raw_columns)
            result["raw_metadata"] = raw_metadata
        return result


@dataclass(frozen=True, slots=True)
class SubdivisionSelection:
    subdivisions: tuple[WorkoutSubdivision, ...]
    total_subdivisions: int
    matched_subdivisions: int
    truncated: bool

    def to_dict(self, *, include_raw_metadata: bool = True) -> dict[str, Any]:
        return {
            "total_subdivisions": self.total_subdivisions,
            "matched_subdivisions": self.matched_subdivisions,
            "returned_subdivisions": len(self.subdivisions),
            "truncated": self.truncated,
            "subdivisions": [
                subdivision.to_dict(include_raw_metadata=include_raw_metadata)
                for subdivision in self.subdivisions
            ],
        }


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.astimezone(UTC)
    return normalized.isoformat().replace("+00:00", "Z")


def _present_metrics(values: dict[str, Any]) -> dict[str, Any]:
    """Round normalized presentation values without mutating raw metadata."""

    return {key: _present_number(key, value) for key, value in values.items()}


def _present_number(key: str, value: Any) -> Any:
    if not isinstance(value, float):
        return value
    if "heart_rate" in key or key in {"swolf", "stroke_count"}:
        return round(value)
    if (
        "stroke_rate" in key
        or "frequency_per_minute" in key
        or "pace_seconds_per_kilometer" in key
        or "pace_seconds_per_100" in key
    ):
        digits = 1
    elif "pace_seconds_per_meter" in key:
        digits = 3
    elif "distance" in key or "elevation" in key or "altitude" in key:
        digits = 2
    elif "offset_seconds" in key or "duration_seconds" in key:
        digits = 3
    elif "energy" in key:
        digits = 2
    elif "speed" in key or "time_seconds" in key:
        digits = 3
    else:
        digits = 3
    rounded = round(value, digits)
    return 0.0 if rounded == 0 else rounded
