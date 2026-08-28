"""Read-only MCP transport for normalized Zepp workout data."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import date
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .client import ZeppApiClient, ZeppApiError
from .config import ConfigError, ZeppConfig
from .models import WorkoutSummary
from .normalizers import NormalizationError
from .service import WorkoutServiceError, ZeppWorkoutService

SERVER_NAME = "zeppgpt"
SERVER_VERSION = "0.1.0"
MAX_MCP_SAMPLE_POINTS = 500
MAX_MCP_SUBDIVISIONS = 200
MAX_RESULT_CHARACTERS = 250_000

SERVER_INSTRUCTIONS = (
    "Read-only access to the configured Zepp account's workout history. Start with "
    "list_workouts to discover stable workout IDs, then pass an ID unchanged to "
    "detail, sample, or lap tools. Unknown Zepp sport types remain available by "
    "numeric ID. Never infer that missing metrics are zero. All tools are read-only."
)

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SampleField = Literal[
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
]


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkoutListItem(OutputModel):
    workout_id: str
    date: str | None
    start_time_utc: str | None
    sport_type: str
    zepp_type_id: int | None
    sport_mode_id: int | None
    duration_seconds: float | None
    distance_meters: float | None
    energy_kilojoules: float | None
    calories_kilocalories: float | None
    average_heart_rate_bpm: int | None
    maximum_heart_rate_bpm: int | None
    source_device: str | None
    training_load: int | None
    aerobic_effect: float | None
    anaerobic_effect: float | None


class WorkoutListOutput(OutputModel):
    count: int
    workouts: list[WorkoutListItem]


class WorkoutOutput(OutputModel):
    workout_id: str
    workout: dict[str, Any]


class WorkoutSamplesOutput(OutputModel):
    workout_id: str
    available_fields: list[str]
    selected_fields: list[str]
    total_samples: int
    matched_samples: int
    returned_samples: int
    downsampled: bool
    samples: list[dict[str, Any]]


class WorkoutSubdivisionsOutput(OutputModel):
    workout_id: str
    available_kinds: list[str]
    total_subdivisions: int
    matched_subdivisions: int
    returned_subdivisions: int
    truncated: bool
    subdivisions: list[dict[str, Any]]


def create_mcp_server(service: ZeppWorkoutService) -> MCPServer:
    """Create an MCP server bound to one configured Zepp account service."""

    server = MCPServer(
        name=SERVER_NAME,
        title="Zepp Workouts",
        description="Read-only access to normalized Zepp and Amazfit workouts.",
        instructions=SERVER_INSTRUCTIONS,
        version=SERVER_VERSION,
        log_level="WARNING",
    )

    @server.tool(
        title="List workouts",
        description=(
            "Use this when the user wants to find, browse, compare, or choose Zepp "
            "workouts. Returns newest first with compact metrics and stable IDs. "
            "sport_type accepts a normalized name, unknown, or a numeric Zepp type ID."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def list_workouts(
        start_date: Annotated[
            date | None,
            Field(description="Optional inclusive workout start date in YYYY-MM-DD."),
        ] = None,
        end_date: Annotated[
            date | None,
            Field(description="Optional inclusive workout start date in YYYY-MM-DD."),
        ] = None,
        sport_type: Annotated[
            str | None,
            Field(
                description=(
                    "Optional normalized sport name, 'unknown', or numeric Zepp "
                    "type ID such as '22'."
                )
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(ge=1, le=100, description="Maximum workouts to return."),
        ] = 20,
    ) -> WorkoutListOutput:
        summaries = _safe_service_call(
            lambda: service.list_workouts(
                start_date=start_date,
                end_date=end_date,
                sport_type=sport_type,
                limit=limit,
            )
        )
        workouts = [_compact_summary(summary) for summary in summaries]
        return WorkoutListOutput(count=len(workouts), workouts=workouts)

    @server.tool(
        title="Get workout",
        description=(
            "Use this when the user wants the complete normalized metadata for one "
            "Zepp workout. Obtain workout_id from list_workouts and pass it unchanged. "
            "Bulk samples and laps are returned by their dedicated tools."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_workout(
        workout_id: Annotated[
            str,
            Field(description="Stable compound ID returned by a workout-list tool."),
        ],
        include_raw_metadata: Annotated[
            bool,
            Field(
                description=(
                    "Include bounded unclassified Zepp fields. Disable for a smaller result."
                )
            ),
        ] = True,
    ) -> WorkoutOutput:
        workout = _safe_service_call(lambda: service.get_workout(workout_id))
        return _workout_output(workout, include_raw_metadata=include_raw_metadata)

    @server.tool(
        title="Get latest workout",
        description=(
            "Use this when the user asks about their most recent Zepp workout, "
            "optionally restricted to a sport. Returns full metadata but not bulk "
            "samples or laps."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_latest_workout(
        sport_type: Annotated[
            str | None,
            Field(
                description=(
                    "Optional normalized sport name, 'unknown', or numeric Zepp type ID."
                )
            ),
        ] = None,
        include_raw_metadata: Annotated[
            bool,
            Field(description="Include bounded unclassified Zepp fields."),
        ] = True,
    ) -> WorkoutOutput:
        workout = _safe_service_call(
            lambda: service.get_latest_workout(sport_type=sport_type)
        )
        return _workout_output(workout, include_raw_metadata=include_raw_metadata)

    @server.tool(
        title="Get workout samples",
        description=(
            "Use this when the user needs time-series measurements or route points "
            "for one workout. Supports time filtering, metric filtering, and bounded "
            "even downsampling. It never interpolates missing GPS coordinates."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_workout_samples(
        workout_id: Annotated[
            str,
            Field(description="Stable compound ID returned by a workout-list tool."),
        ],
        fields: Annotated[
            list[SampleField] | None,
            Field(
                description=(
                    "Optional sample metrics to retain. Samples without any selected "
                    "metric are filtered out."
                )
            ),
        ] = None,
        start_offset_seconds: Annotated[
            int | None,
            Field(ge=0, description="Optional inclusive offset from workout start."),
        ] = None,
        end_offset_seconds: Annotated[
            int | None,
            Field(ge=0, description="Optional inclusive offset from workout start."),
        ] = None,
        max_points: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_MCP_SAMPLE_POINTS,
                description="Maximum samples after filtering and even downsampling.",
            ),
        ] = 200,
    ) -> WorkoutSamplesOutput:
        selected_fields = tuple(fields or ())
        selection, available_fields = _safe_service_call(
            lambda: service.get_workout_samples(
                workout_id,
                start_offset_seconds=start_offset_seconds,
                end_offset_seconds=end_offset_seconds,
                require_fields=selected_fields,
                max_points=max_points,
            )
        )
        output_fields = (
            frozenset(selected_fields)
            if selected_fields
            else frozenset(available_fields)
        )
        serialized = selection.to_dict(fields=output_fields)
        return WorkoutSamplesOutput(
            workout_id=workout_id,
            available_fields=list(available_fields),
            selected_fields=list(selected_fields or available_fields),
            total_samples=serialized["total_samples"],
            matched_samples=serialized["matched_samples"],
            returned_samples=serialized["returned_samples"],
            downsampled=serialized["downsampled"],
            samples=serialized["samples"],
        )

    @server.tool(
        title="Get workout laps and subdivisions",
        description=(
            "Use this when the user asks for laps, kilometre splits, pool lengths, "
            "intervals, pauses, or strength sets. Returns every available subdivision "
            "kind without assuming all sports share one lap model."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_workout_laps(
        workout_id: Annotated[
            str,
            Field(description="Stable compound ID returned by a workout-list tool."),
        ],
        kinds: Annotated[
            list[str] | None,
            Field(description="Optional exact subdivision kinds to retain."),
        ] = None,
        max_records: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_MCP_SUBDIVISIONS,
                description="Maximum subdivision records to return.",
            ),
        ] = 100,
    ) -> WorkoutSubdivisionsOutput:
        selection, available_kinds = _safe_service_call(
            lambda: service.get_workout_subdivisions(
                workout_id,
                kinds=tuple(kinds or ()),
                max_records=max_records,
            )
        )
        # Positional source rows remain available to the development inspector,
        # but regular MCP consumers receive the semantic subdivision model.
        serialized = selection.to_dict(include_raw_metadata=False)
        return WorkoutSubdivisionsOutput(
            workout_id=workout_id,
            available_kinds=list(available_kinds),
            total_subdivisions=serialized["total_subdivisions"],
            matched_subdivisions=serialized["matched_subdivisions"],
            returned_subdivisions=serialized["returned_subdivisions"],
            truncated=serialized["truncated"],
            subdivisions=[
                _bounded_value(item) for item in serialized["subdivisions"]
            ],
        )

    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zepp-mcp",
        description="Run the read-only Zepp workout MCP server.",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--transport",
        choices=("streamable-http", "stdio"),
        default="streamable-http",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--allow-network-bind",
        action="store_true",
        help="Allow an unauthenticated bind beyond localhost (not recommended).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 1 <= args.port <= 65535:
            raise ConfigError("port must be between 1 and 65535")
        if (
            args.transport == "streamable-http"
            and args.host not in {"127.0.0.1", "localhost", "::1"}
            and not args.allow_network_bind
        ):
            raise ConfigError(
                "Refusing unauthenticated non-local bind; use Secure MCP Tunnel or "
                "pass --allow-network-bind explicitly"
            )
        config = ZeppConfig.load(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    service = ZeppWorkoutService(ZeppApiClient(config))
    server = create_mcp_server(service)
    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            server.run(
                "streamable-http",
                host=args.host,
                port=args.port,
                streamable_http_path="/mcp",
                max_request_body_size=1_048_576,
            )
    except KeyboardInterrupt:
        return 130
    return 0


def _compact_summary(summary: WorkoutSummary) -> WorkoutListItem:
    payload = summary.to_dict(include_raw_metadata=False)
    energy = payload["energy_kilojoules"]
    return WorkoutListItem(
        workout_id=summary.workout_id.encoded,
        date=(
            summary.start_time_utc.date().isoformat()
            if summary.start_time_utc is not None
            else None
        ),
        start_time_utc=payload["start_time_utc"],
        sport_type=summary.workout_type.name,
        zepp_type_id=summary.workout_type.zepp_type_id,
        sport_mode_id=summary.sport_mode_id,
        duration_seconds=payload["duration_seconds"],
        distance_meters=payload["distance_meters"],
        energy_kilojoules=energy,
        calories_kilocalories=(
            None if energy is None else round(energy / 4.184, 2)
        ),
        average_heart_rate_bpm=summary.average_heart_rate_bpm,
        maximum_heart_rate_bpm=summary.maximum_heart_rate_bpm,
        source_device=summary.device_name,
        training_load=summary.training_load,
        aerobic_effect=payload["aerobic_effect"],
        anaerobic_effect=payload["anaerobic_effect"],
    )


def _workout_output(workout, *, include_raw_metadata: bool) -> WorkoutOutput:
    payload = workout.to_dict(include_raw_metadata=include_raw_metadata)
    payload["derived_metrics"] = _derived_metrics(workout.summary)
    bounded = _bounded_value(payload)
    if len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))) > MAX_RESULT_CHARACTERS:
        bounded = workout.to_dict(include_raw_metadata=False)
        bounded["derived_metrics"] = _derived_metrics(workout.summary)
        bounded["raw_metadata_notice"] = (
            "Unclassified metadata was omitted because the bounded result was too large."
        )
    return WorkoutOutput(
        workout_id=workout.workout_id.encoded,
        workout=bounded,
    )


def _derived_metrics(summary: WorkoutSummary) -> dict[str, float]:
    duration = summary.duration_seconds
    distance = summary.distance_meters
    if duration is None or duration <= 0 or distance is None or distance <= 0:
        return {}
    result = {
        "average_speed_kilometers_per_hour": round(
            distance / duration * 3.6, 3
        ),
    }
    if summary.workout_type.category in {"running", "walking", "hiking"}:
        result["average_pace_seconds_per_kilometer"] = round(
            duration / distance * 1000, 1
        )
    if summary.workout_type.category == "swimming":
        result["average_pace_seconds_per_100_meters"] = round(
            duration / distance * 100, 1
        )
    return result


T = TypeVar("T")


def _safe_service_call(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (
        NormalizationError,
        WorkoutServiceError,
        ZeppApiError,
        ValueError,
    ) as exc:
        raise ToolError(str(exc)) from None


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return {"truncated": True, "reason": "maximum nesting depth"}
    if isinstance(value, str):
        if len(value) <= 1_000:
            return value
        return {
            "truncated": True,
            "original_characters": len(value),
            "prefix": value[:1_000],
        }
    if isinstance(value, list | tuple):
        items = [_bounded_value(item, depth=depth + 1) for item in value[:200]]
        if len(value) > 200:
            items.append({"truncated_items": len(value) - 200})
        return items
    if isinstance(value, dict):
        items = list(value.items())
        result = {
            str(key): _bounded_value(item, depth=depth + 1)
            for key, item in items[:200]
        }
        if len(items) > 200:
            result["_truncated_fields"] = len(items) - 200
        return result
    return value


if __name__ == "__main__":
    raise SystemExit(main())
