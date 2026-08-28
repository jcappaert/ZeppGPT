"""Command-line interface for the Zepp workout API client."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any

from .client import ZeppApiClient, ZeppApiError, compact_workout
from .config import ConfigError, ZeppConfig
from .diagnostics import (
    create_diagnostic_directory,
    field_inventory,
    write_json,
)
from .inspection import build_workout_inspection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zeppgpt",
        description="Inspect and normalize workout data from a Zepp account.",
    )
    parser.add_argument(
        "--env-file", default=".env", help="Local env file (default: .env)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "doctor", help="Check local configuration without network access"
    )

    probe = subparsers.add_parser(
        "probe", help="Validate auth and inspect workout payloads"
    )
    probe.add_argument("--limit", type=int, default=20, help="Maximum workouts to list")
    probe.add_argument(
        "--details", type=int, default=5, help="Maximum details to fetch"
    )
    probe.add_argument("--max-pages", type=int, default=3, help="Maximum history pages")
    probe.add_argument("--start-date", type=_iso_date)
    probe.add_argument("--end-date", type=_iso_date)
    probe.add_argument("--save-raw", action="store_true")
    probe.add_argument("--output-dir", default="diagnostics")

    detail = subparsers.add_parser("detail", help="Fetch a single known workout detail")
    detail.add_argument("--track-id", required=True)
    detail.add_argument("--source", required=True)
    detail.add_argument("--save-raw", action="store_true")
    detail.add_argument("--output-dir", default="diagnostics")

    inspect_workout = subparsers.add_parser(
        "inspect-workout",
        help="Inspect normalization, subdivisions, and positional columns",
    )
    inspect_workout.add_argument("--track-id", required=True)
    inspect_workout.add_argument(
        "--source",
        help="Zepp source identifier; inferred from bounded history when omitted",
    )
    inspect_workout.add_argument("--max-pages", type=int, default=10)
    inspect_workout.add_argument("--sample-limit", type=int, default=5)
    inspect_workout.add_argument("--subdivision-limit", type=int, default=200)
    inspect_workout.add_argument(
        "--include-raw",
        action="store_true",
        help="Include raw summary and subdivision rows in this local report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = ZeppConfig.load(
            args.env_file,
            require_credentials=args.command != "doctor",
        )
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "probe":
            return _probe(config, args)
        if args.command == "detail":
            return _detail(config, args)
        if args.command == "inspect-workout":
            return _inspect_workout(config, args)
    except (ConfigError, ValueError) as exc:
        print(f"Configuration/input error: {exc}", file=sys.stderr)
        return 2
    except ZeppApiError as exc:
        location = ""
        if exc.host or exc.path:
            location = f" [{exc.host or ''}{exc.path or ''}]"
        print(f"Zepp probe failed{location}: {exc}", file=sys.stderr)
        return 1
    return 2


def _doctor(config: ZeppConfig) -> int:
    token_state = "configured" if config.app_token else "missing"
    user_state = "configured" if config.user_id else "missing"
    print("ZeppGPT configuration")
    print(f"  ZEPP_APP_TOKEN: {token_state}")
    print(f"  ZEPP_USER_ID: {user_state}")
    print(f"  API hosts: {', '.join(config.api_hosts)}")
    print(f"  Source: {config.source}")
    for warning in config.warnings():
        print(f"  Warning: {warning}")
    if not config.app_token or not config.user_id:
        print("  Status: incomplete; copy .env.example to .env and add your credentials")
        return 1
    print("  Status: ready for a read-only network probe")
    return 0


def _probe(config: ZeppConfig, args: argparse.Namespace) -> int:
    if not 0 <= args.details <= 50:
        raise ValueError("details must be between 0 and 50")
    client = ZeppApiClient(config)
    host, first_page = client.probe_hosts()
    workouts, pages = client.list_workouts(
        limit=args.limit,
        max_pages=args.max_pages,
        start_date=args.start_date,
        end_date=args.end_date,
        first_page=first_page,
    )

    details: list[dict[str, Any]] = []
    for workout in _select_detail_candidates(workouts, args.details):
        track_id = str(workout.get("trackid", ""))
        source = str(workout.get("source") or config.source)
        if not track_id:
            continue
        try:
            payload = client.get_workout_detail(track_id=track_id, source=source)
        except ZeppApiError as exc:
            details.append(
                {
                    "track_id": track_id,
                    "source": source,
                    "error": str(exc),
                    "status": exc.status,
                }
            )
            continue
        details.append(
            {
                "track_id": track_id,
                "source": source,
                "type_id": workout.get("type"),
                "sport_mode": workout.get("sport_mode"),
                "payload": payload,
                "inventory": field_inventory(payload),
                "fields_present": _field_presence(payload),
            }
        )

    type_counts = Counter(str(item.get("type", "unknown")) for item in workouts)
    result = {
        "authentication": "ok",
        "active_host": host,
        "history_pages": len(pages),
        "workout_count": len(workouts),
        "workout_type_counts": dict(sorted(type_counts.items())),
        "workouts": [compact_workout(item) for item in workouts],
        "details": [
            {
                key: value
                for key, value in detail.items()
                if key not in {"payload", "inventory"}
            }
            for detail in details
        ],
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if args.save_raw:
        directory = create_diagnostic_directory(args.output_dir)
        secrets = (config.app_token, config.user_id)
        for index, payload in enumerate(pages, start=1):
            write_json(
                directory / f"history-page-{index}.json", payload, secrets=secrets
            )
        write_json(
            directory / "history-inventory.json",
            field_inventory(pages),
            secrets=secrets,
        )
        for detail in details:
            if "payload" not in detail:
                continue
            safe_track_id = _filename_component(detail["track_id"])
            write_json(
                directory / f"detail-{safe_track_id}.json",
                detail["payload"],
                secrets=secrets,
            )
            write_json(
                directory / f"detail-{safe_track_id}-inventory.json",
                detail["inventory"],
                secrets=secrets,
            )
        write_json(directory / "report.json", result, secrets=secrets)
        print(f"Saved sensitive diagnostic data under {directory}", file=sys.stderr)
    return 0


def _detail(config: ZeppConfig, args: argparse.Namespace) -> int:
    client = ZeppApiClient(config)
    host, _ = client.probe_hosts()
    payload = client.get_workout_detail(track_id=args.track_id, source=args.source)
    report = {
        "authentication": "ok",
        "active_host": host,
        "track_id": args.track_id,
        "source": args.source,
        "inventory": field_inventory(payload),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.save_raw:
        directory = create_diagnostic_directory(args.output_dir)
        secrets = (config.app_token, config.user_id)
        safe_track_id = _filename_component(args.track_id)
        write_json(directory / f"detail-{safe_track_id}.json", payload, secrets=secrets)
        write_json(directory / "report.json", report, secrets=secrets)
        print(f"Saved sensitive diagnostic data under {directory}", file=sys.stderr)
    return 0


def _inspect_workout(config: ZeppConfig, args: argparse.Namespace) -> int:
    if not 1 <= args.max_pages <= 100:
        raise ValueError("max-pages must be between 1 and 100")
    client = ZeppApiClient(config)
    host, first_page = client.probe_hosts()
    workouts, _ = client.list_workouts(
        limit=500,
        max_pages=args.max_pages,
        first_page=first_page,
    )
    summary = next(
        (
            workout
            for workout in workouts
            if str(workout.get("trackid")) == args.track_id
            and (
                args.source is None
                or str(workout.get("source")) == args.source
            )
        ),
        None,
    )
    if summary is None:
        raise ValueError("workout was not found in bounded Zepp history")
    source = str(summary.get("source") or args.source or config.source)
    detail = client.get_workout_detail(track_id=args.track_id, source=source)
    report = build_workout_inspection(
        summary,
        detail,
        sample_limit=args.sample_limit,
        subdivision_limit=args.subdivision_limit,
        include_raw=args.include_raw,
    )
    report["authentication"] = "ok"
    report["active_host"] = host
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _filename_component(value: str) -> str:
    cleaned = "".join(
        character for character in value if character.isalnum() or character in "-_"
    )
    return cleaned[:80] or "unknown"


def _field_presence(payload: dict[str, Any]) -> dict[str, Any]:
    """Describe populated top-level detail fields without exposing values."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    names: list[str] = []
    large_value_lengths: dict[str, int] = {}
    for name, value in sorted(data.items()):
        if name in {"trackid", "source"} or value in (None, "", [], {}):
            continue
        names.append(name)
        if isinstance(value, (str, list, dict)) and len(value) >= 100:
            large_value_lengths[name] = len(value)
    return {
        "names": names,
        "large_value_lengths": large_value_lengths,
    }


def _select_detail_candidates(
    workouts: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Prefer one representative of each observed sport before filling slots."""
    if limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    selected_ids: set[tuple[str, str]] = set()
    seen_sports: set[tuple[str, str]] = set()

    for workout in workouts:
        sport_signature = (
            str(workout.get("type", "unknown")),
            str(workout.get("sport_mode", "unknown")),
        )
        if sport_signature in seen_sports:
            continue
        seen_sports.add(sport_signature)
        identity = (
            str(workout.get("trackid", "")),
            str(workout.get("source", "")),
        )
        selected.append(workout)
        selected_ids.add(identity)
        if len(selected) >= limit:
            return selected

    for workout in workouts:
        identity = (
            str(workout.get("trackid", "")),
            str(workout.get("source", "")),
        )
        if identity in selected_ids:
            continue
        selected.append(workout)
        selected_ids.add(identity)
        if len(selected) >= limit:
            break
    return selected


if __name__ == "__main__":
    raise SystemExit(main())
