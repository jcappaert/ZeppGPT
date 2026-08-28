import json
import unittest
from pathlib import Path

from zeppgpt.inspection import build_workout_inspection
from zeppgpt.normalizers import (
    normalize_samples,
    normalize_subdivisions,
    normalize_summary,
    normalize_workout,
)

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "zepp_semantic_regressions.json"
)


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def positional_series(columns, records, width):
    rows = []
    for values in records:
        row = ["0"] * width
        for column, value in zip(columns, values, strict=True):
            row[column] = str(value)
        rows.append(",".join(row))
    return ";".join(rows) + ";"


def pool_payload(fixture):
    summary = fixture["summary"]
    spec = fixture["length_generation"]
    lengths = []
    for index in range(spec["count"]):
        breaststroke = index < spec["breaststroke_lengths"]
        stroke_type_id = 1 if breaststroke else 2
        strokes = (
            spec["breaststroke_strokes"]
            if breaststroke
            else spec["freestyle_strokes"]
        )
        duration_milliseconds = spec["duration_milliseconds"]
        duration_seconds = duration_milliseconds / 1000
        end_milliseconds = (
            spec["first_end_milliseconds"]
            + index * spec["completion_interval_milliseconds"]
        )
        distance = spec["distance_meters"]
        lengths.append(
            [
                index,
                round(duration_seconds),
                distance,
                spec["average_heart_rate_bpm"],
                round(end_milliseconds / 1000),
                duration_seconds / distance,
                spec["stroke_rate_per_second"],
                strokes,
                round(duration_seconds) + strokes,
                spec["energy_kilocalories"],
                7,
                stroke_type_id,
                duration_milliseconds,
                round(distance / strokes, 2),
                end_milliseconds,
                spec["maximum_heart_rate_bpm"],
            ]
        )
    lap = positional_series(
        fixture["columns"], fixture["groups"] + lengths, 70
    )
    pause_start = int(summary["trackid"]) + fixture["pause_offset_seconds"]
    pause = (
        f"{pause_start},{fixture['pause_duration_seconds']},-1,-1,2;"
    )
    return {
        "code": 1,
        "data": {
            "trackid": int(summary["trackid"]),
            "source": summary["source"],
            "lap": lap,
            "pause": pause,
        },
    }


def run_payload(fixture):
    summary = fixture["summary"]
    time = ";".join(["0"] + ["1"] * (len(fixture["altitudes_centimeters"]) - 1))
    kilo_pace = positional_series(
        fixture["split_columns"], fixture["splits"], 15
    )
    return {
        "code": 1,
        "data": {
            "trackid": int(summary["trackid"]),
            "source": summary["source"],
            "time": time + ";",
            "altitude": ";".join(
                str(value) for value in fixture["altitudes_centimeters"]
            )
            + ";",
            "currentDistance": (
                f"{int(summary['run_time']) - 1},0;"
                f"0,{int(summary['dis'] * 100)};"
            ),
            "kilo_pace": kilo_pace,
        },
    }


def rowing_payload(fixture):
    lap = positional_series(fixture["lap_columns"], [fixture["lap"]], 70)
    track_id = int(fixture["summary"]["trackid"])
    pause = ";".join(
        f"{track_id + offset},{duration},-1,-1,{pause_type}"
        for offset, duration, pause_type in fixture["pauses"]
    )
    return {
        "code": 1,
        "data": {
            "trackid": track_id,
            "source": fixture["summary"]["source"],
            "lap": lap,
            "pause": pause + ";",
        },
    }


class SyntheticSemanticRegressionTests(unittest.TestCase):
    def test_1500_meter_pool_swim_decodes_lengths_sets_styles_and_pause(self):
        fixture = load_fixture()["pool_synthetic_1500"]
        summary = normalize_summary(fixture["summary"])
        payload = pool_payload(fixture)

        subdivisions = normalize_subdivisions(payload, summary=summary)
        lengths = [item for item in subdivisions if item.kind == "pool_length"]
        sets = [item for item in subdivisions if item.kind == "pool_set"]
        pauses = [item for item in subdivisions if item.kind == "pause"]

        self.assertEqual(len(lengths), 60)
        self.assertEqual(len(sets), 3)
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0].metrics["pause_type"], "manual")
        self.assertAlmostEqual(
            sum(item.metrics["distance_meters"] for item in lengths), 1500
        )
        self.assertEqual(
            sum(item.metrics["stroke_count"] for item in lengths), 640
        )
        by_stroke = {
            stroke: sum(
                item.metrics["distance_meters"]
                for item in lengths
                if item.metrics["stroke_type"] == stroke
            )
            for stroke in ("freestyle", "breaststroke")
        }
        self.assertEqual(by_stroke, {"freestyle": 1000, "breaststroke": 500})
        self.assertEqual(
            summary.sport_metrics["freestyle_distance_meters"], 1000
        )
        self.assertEqual(
            summary.sport_metrics["breaststroke_distance_meters"], 500
        )

        wall_duration = (
            summary.end_time_utc - summary.start_time_utc
        ).total_seconds()
        self.assertTrue(
            all(
                item.end_offset_seconds is None
                or item.end_offset_seconds <= wall_duration
                for item in subdivisions
            )
        )
        self.assertTrue(all(item.metrics["distance_meters"] == 500 for item in sets))
        self.assertEqual(
            [item.duration_seconds for item in sets],
            [600, 600, 600],
        )
        for set_index, pool_set in enumerate(sets):
            members = lengths[set_index * 20 : (set_index + 1) * 20]
            self.assertAlmostEqual(
                pool_set.start_offset_seconds,
                min(item.start_offset_seconds for item in members),
            )
            self.assertAlmostEqual(
                pool_set.end_offset_seconds,
                max(item.end_offset_seconds for item in members),
            )
            self.assertEqual(
                pool_set.provenance["start_offset_seconds"], "derived"
            )
            self.assertEqual(
                pool_set.provenance["end_offset_seconds"], "derived"
            )
            self.assertEqual(
                pool_set.metrics["stroke_count"],
                sum(item.metrics["stroke_count"] for item in members),
            )
        self.assertAlmostEqual(lengths[-1].end_offset_seconds, wall_duration)
        self.assertLessEqual(
            max(item.end_offset_seconds for item in sets),
            lengths[-1].end_offset_seconds,
        )

        first = lengths[0]
        self.assertEqual(first.metrics["stroke_type_id"], 1)
        self.assertEqual(first.metrics["stroke_type"], "breaststroke")
        self.assertEqual(first.metrics["stroke_count"], 12)
        self.assertEqual(first.metrics["swolf"], 42)
        self.assertEqual(first.metrics["average_heart_rate_bpm"], 120)
        self.assertEqual(first.metrics["maximum_heart_rate_bpm"], 140)
        self.assertAlmostEqual(first.duration_seconds, 30)
        self.assertAlmostEqual(first.metrics["pace_seconds_per_100_meters"], 120)
        self.assertAlmostEqual(
            first.metrics["distance_per_stroke_meters"], 25 / 12
        )

        for length in lengths:
            pace_per_meter = length.metrics["pace_seconds_per_100_meters"] / 100
            self.assertAlmostEqual(
                pace_per_meter * length.metrics["distance_meters"],
                length.duration_seconds,
                delta=0.05,
            )
        self.assertGreaterEqual(
            sum(
                abs(
                    item.metrics["swolf"]
                    - (item.duration_seconds + item.metrics["stroke_count"])
                )
                <= 4
                for item in lengths
            ),
            59,
        )

        serialized = first.to_dict(include_raw_metadata=False)
        self.assertNotIn("raw_columns", serialized)
        self.assertNotIn("raw_metadata", serialized)
        debug = first.to_dict(include_raw_metadata=True)
        self.assertIn("raw_columns", debug["raw_metadata"])
        noisy_rate = next(
            item
            for item in lengths
            if 24.9 < item.metrics["stroke_rate_per_minute"] < 25.1
        )
        self.assertEqual(
            noisy_rate.to_dict(include_raw_metadata=False)["metrics"][
                "stroke_rate_per_minute"
            ],
            25.0,
        )
        report = build_workout_inspection(fixture["summary"], payload)
        self.assertEqual(report["workout_wall_clock_duration_seconds"], 2100)
        self.assertTrue(
            any("wall clock" in warning for warning in report["warnings"])
        )

    def test_run_elevation_scale_samples_and_kilometer_splits(self):
        fixture = load_fixture()["run_synthetic_8km"]
        summary = normalize_summary(fixture["summary"])
        payload = run_payload(fixture)
        workout = normalize_workout(fixture["summary"], payload)

        self.assertAlmostEqual(summary.elevation_gain_meters, 50)
        self.assertAlmostEqual(summary.elevation_loss_meters, 45)
        self.assertEqual(summary.provenance["elevation_gain_meters"], "decoded")
        self.assertLess(workout.summary.elevation_gain_meters, 100)

        samples = normalize_samples(payload)
        self.assertAlmostEqual(samples[-1].distance_meters, 8000)
        altitudes = [
            sample.altitude_meters
            for sample in samples
            if sample.altitude_meters is not None
        ]
        self.assertGreaterEqual(min(altitudes), 100)
        self.assertLessEqual(max(altitudes), 130)

        subdivisions = normalize_subdivisions(payload, summary=summary)
        splits = [item for item in subdivisions if item.kind == "kilometer_split"]
        self.assertEqual(len(splits), 8)
        split_distance = sum(item.metrics["distance_meters"] for item in splits)
        self.assertLessEqual(summary.distance_meters - split_distance, 1000)
        self.assertAlmostEqual(
            splits[0].metrics["pace_seconds_per_kilometer"], 350
        )
        self.assertEqual(splits[0].metrics["cadence_per_minute"], 150)
        self.assertAlmostEqual(
            summary.sport_metrics["average_pace_seconds_per_meter"],
            summary.duration_seconds / summary.distance_meters,
        )

    def test_type_23_is_rowing_and_uses_rowing_specific_subdivisions(self):
        fixture = load_fixture()["rowing_synthetic_type_23"]
        summary = normalize_summary(fixture["summary"])
        payload = rowing_payload(fixture)

        self.assertTrue(summary.workout_type.known)
        self.assertEqual(summary.workout_type.name, "rowing_machine")
        self.assertEqual(summary.workout_type.category, "rowing")
        self.assertEqual(summary.sport_metrics["stroke_count"], 1200)
        self.assertEqual(
            summary.sport_metrics["average_stroke_rate_per_minute"], 25
        )

        subdivisions = normalize_subdivisions(payload, summary=summary)
        pauses = [item for item in subdivisions if item.kind == "pause"]
        intervals = [
            item for item in subdivisions if item.kind == "rowing_interval"
        ]
        self.assertEqual(len(pauses), 3)
        self.assertTrue(
            all(item.metrics["pause_type"] == "manual" for item in pauses)
        )
        self.assertEqual(len(intervals), 1)
        interval = intervals[0]
        self.assertEqual(interval.kind, "rowing_interval")
        self.assertEqual(interval.metrics["stroke_count"], 1200)
        self.assertEqual(interval.metrics["stroke_rate_per_minute"], 25)
        self.assertEqual(interval.metrics["distance_meters"], 10000)
        self.assertAlmostEqual(interval.duration_seconds, 2850)
        self.assertIsNone(interval.start_offset_seconds)
        self.assertIsNone(interval.end_offset_seconds)
        self.assertNotIn("start_offset_seconds", interval.provenance)
        self.assertNotIn("end_offset_seconds", interval.provenance)

        wall_duration = (
            summary.end_time_utc - summary.start_time_utc
        ).total_seconds()
        self.assertTrue(
            all(
                item.end_offset_seconds is None
                or item.end_offset_seconds <= wall_duration
                for item in subdivisions
            )
        )

    def test_type_22_is_evidence_backed_hiking(self):
        fixture = load_fixture()["hiking_synthetic_type_22"]
        summary = normalize_summary(fixture["summary"])

        self.assertTrue(summary.workout_type.known)
        self.assertEqual(summary.workout_type.name, "hiking")
        self.assertEqual(summary.workout_type.category, "hiking")
        self.assertAlmostEqual(summary.elevation_gain_meters, 400)
        self.assertEqual(summary.total_steps, 12000)

    def test_all_semantic_subdivision_timing_invariants(self):
        fixture = load_fixture()
        cases = [
            (
                fixture["pool_synthetic_1500"],
                pool_payload(fixture["pool_synthetic_1500"]),
            ),
            (
                fixture["run_synthetic_8km"],
                run_payload(fixture["run_synthetic_8km"]),
            ),
            (
                fixture["rowing_synthetic_type_23"],
                rowing_payload(fixture["rowing_synthetic_type_23"]),
            ),
        ]
        wall_duration_kinds = {
            "pause",
            "pool_length",
            "kilometer_split",
            "mile_split",
        }
        for case, payload in cases:
            summary = normalize_summary(case["summary"])
            wall_duration = (
                summary.end_time_utc - summary.start_time_utc
            ).total_seconds()
            for item in normalize_subdivisions(payload, summary=summary):
                if item.start_offset_seconds is not None:
                    self.assertGreaterEqual(item.start_offset_seconds, 0)
                if item.end_offset_seconds is not None:
                    self.assertLessEqual(item.end_offset_seconds, wall_duration)
                if (
                    item.start_offset_seconds is not None
                    and item.end_offset_seconds is not None
                ):
                    self.assertLessEqual(
                        item.start_offset_seconds, item.end_offset_seconds
                    )
                if (
                    item.kind in wall_duration_kinds
                    and item.start_offset_seconds is not None
                    and item.end_offset_seconds is not None
                    and item.duration_seconds is not None
                ):
                    self.assertAlmostEqual(
                        item.end_offset_seconds - item.start_offset_seconds,
                        item.duration_seconds,
                        delta=2.0,
                    )

    def test_inspection_report_is_bounded_and_hides_coordinates(self):
        fixture = load_fixture()["run_synthetic_8km"]
        report = build_workout_inspection(
            fixture["summary"], run_payload(fixture), sample_limit=2
        )

        self.assertEqual(len(report["sample_preview"]), 2)
        self.assertNotIn("latitude_degrees", report["sample_preview"][0])
        self.assertIn("kilo_pace", report["column_analysis"])
        self.assertNotIn("raw_summary", report)


if __name__ == "__main__":
    unittest.main()
