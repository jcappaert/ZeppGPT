import json
import unittest
from copy import deepcopy
from pathlib import Path

from zeppgpt.models import WorkoutId
from zeppgpt.normalizers import (
    NormalizationError,
    normalize_samples,
    normalize_subdivisions,
    normalize_summary,
    normalize_workout,
    select_samples,
    select_subdivisions,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "zepp_workout_shapes.json"


def load_shapes():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def materialize_workout(shape_name):
    fixture = load_shapes()
    record = next(
        workout for workout in fixture["workouts"] if workout["shape"] == shape_name
    )
    summary = deepcopy(record["summary"])
    detail = deepcopy(fixture["templates"][record["template"]])
    detail.update(deepcopy(record.get("detail_extras", {})))
    track_id = summary["trackid"]
    replacements = {
        "{{pause}}": f"{int(track_id) + 10},20,1,2,2;",
        "{{lap70}}": ",".join(str(index) for index in range(70)) + ";",
        "{{kilo15}}": ",".join(str(index) for index in range(15)) + ";",
    }
    for key, value in list(detail.items()):
        if isinstance(value, str) and value in replacements:
            detail[key] = replacements[value]
    detail["trackid"] = int(track_id)
    detail["source"] = summary["source"]
    return summary, {"code": 1, "data": detail}


class WorkoutIdTests(unittest.TestCase):
    def test_compound_id_round_trips_without_delimiter_assumptions(self) -> None:
        workout_id = WorkoutId("track:with:punctuation", "source/with|punctuation")

        restored = WorkoutId.parse(workout_id.encoded)

        self.assertEqual(restored, workout_id)

    def test_invalid_compound_id_has_safe_generic_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid Zepp workout ID"):
            WorkoutId.parse("not-a-zepp-id")


class NormalizerTests(unittest.TestCase):
    def test_all_six_observed_shapes_normalize_offline(self) -> None:
        fixture = load_shapes()
        normalized = []
        for record in fixture["workouts"]:
            summary, detail = materialize_workout(record["shape"])
            normalized.append(
                (
                    normalize_workout(summary, detail),
                    normalize_samples(detail),
                    normalize_subdivisions(
                        detail, summary=normalize_summary(summary)
                    ),
                )
            )

        self.assertEqual(len(normalized), 6)
        self.assertTrue(all(workout.workout_id.encoded for workout, _, _ in normalized))

    def test_evidence_backed_and_unknown_workout_types_remain_distinct(self) -> None:
        expected = {
            "running": ("running", True),
            "walking": ("walking", True),
            "pool_mode_0": ("indoor_pool_swimming", True),
            "pool_mode_5": ("indoor_pool_swimming", True),
            "hiking_22": ("hiking", True),
            "unknown_non_gps_23": ("rowing_machine", True),
        }

        for shape, interpretation in expected.items():
            summary, _ = materialize_workout(shape)
            normalized = normalize_summary(summary)
            self.assertEqual(
                (normalized.workout_type.name, normalized.workout_type.known),
                interpretation,
            )

        unknown_summary, _ = materialize_workout("running")
        unknown_summary["type"] = 999
        unknown = normalize_summary(unknown_summary)
        self.assertEqual(unknown.workout_type.name, "unknown")
        self.assertFalse(unknown.workout_type.known)

    def test_summary_uses_track_id_as_start_and_preserves_unknown_metadata(
        self,
    ) -> None:
        summary, _ = materialize_workout("running")
        summary["end_time"] = str(int(summary["trackid"]) + 900)
        summary["appToken"] = "must-not-survive"

        normalized = normalize_summary(summary)

        self.assertEqual(int(normalized.start_time_utc.timestamp()), 1_700_000_000)
        self.assertEqual(normalized.duration_seconds, 600)
        self.assertEqual(normalized.distance_meters, 1000)
        self.assertAlmostEqual(normalized.energy_kilojoules, 418.4)
        self.assertEqual(normalized.training_load, 50)
        self.assertEqual(normalized.aerobic_effect, 3.0)
        self.assertEqual(normalized.anaerobic_effect, 2.0)
        self.assertEqual(
            normalized.sport_metrics["average_stride_length_meters"], 0.8
        )
        self.assertEqual(
            normalized.raw_metadata["future_summary_field"], "preserve-me"
        )
        self.assertEqual(normalized.raw_metadata["appToken"], "<redacted>")

    def test_swimming_metrics_are_normalized_without_naming_sport_mode(self) -> None:
        summary, _ = materialize_workout("pool_mode_5")

        normalized = normalize_summary(summary)

        self.assertEqual(normalized.sport_mode_id, 5)
        self.assertEqual(normalized.sport_metrics["pool_length_meters"], 50)
        self.assertEqual(normalized.sport_metrics["swim_style_id"], 2)
        self.assertNotIn("sport_mode", normalized.sport_metrics)

    def test_workout_separates_bulk_series_and_retains_unknown_detail_fields(
        self,
    ) -> None:
        summary, detail = materialize_workout("running")

        normalized = normalize_workout(summary, detail)

        self.assertIn("heart_rate_bpm", normalized.available_sample_fields)
        self.assertIn("latitude_degrees", normalized.available_sample_fields)
        self.assertIn(
            "kilometer_split", normalized.available_subdivision_kinds
        )
        self.assertIn("runPosture", normalized.raw_metadata)
        self.assertIn("future_route_series", normalized.raw_metadata)
        self.assertNotIn("heart_rate", normalized.raw_metadata)

    def test_route_and_sparse_series_join_by_accumulated_time(self) -> None:
        _, detail = materialize_workout("running")

        samples = normalize_samples(detail)
        by_offset = {sample.offset_seconds: sample for sample in samples}

        self.assertEqual(sorted(by_offset), [0, 1, 2, 3, 4])
        self.assertEqual(by_offset[4].distance_meters, 10)
        self.assertEqual(by_offset[4].heart_rate_bpm, 102)
        self.assertEqual(by_offset[2].cumulative_steps, 3)
        self.assertEqual(by_offset[2].stride_length_meters, 0.82)
        self.assertIsNone(by_offset[2].latitude_degrees)
        self.assertIsNone(by_offset[3].latitude_degrees)

    def test_non_gps_samples_are_retained_without_coordinates(self) -> None:
        _, detail = materialize_workout("unknown_non_gps_23")

        samples = normalize_samples(detail)

        self.assertTrue(samples)
        self.assertTrue(all(sample.latitude_degrees is None for sample in samples))
        self.assertTrue(any(sample.distance_meters is not None for sample in samples))

    def test_sample_selection_filters_and_preserves_range_endpoints(self) -> None:
        _, detail = materialize_workout("running")
        samples = normalize_samples(detail)

        selection = select_samples(
            samples,
            require_fields=("latitude_degrees",),
            max_points=2,
        )

        self.assertEqual(selection.matched_samples, 3)
        self.assertTrue(selection.downsampled)
        self.assertEqual(
            [sample.offset_seconds for sample in selection.samples], [0, 4]
        )

    def test_subdivisions_decode_pause_and_preserve_wide_columns(self) -> None:
        summary, detail = materialize_workout("pool_mode_0")

        subdivisions = normalize_subdivisions(
            detail, summary=normalize_summary(summary)
        )
        pause = next(item for item in subdivisions if item.kind == "pause")
        lap = next(item for item in subdivisions if item.kind == "pool_record")

        self.assertEqual(pause.start_offset_seconds, 10)
        self.assertEqual(pause.duration_seconds, 20)
        self.assertEqual(pause.metrics["pause_type"], "manual")
        self.assertEqual(len(lap.raw_columns), 70)
        self.assertEqual(lap.raw_metadata["column_count"], 70)

    def test_subdivision_selection_filters_and_caps_independently(self) -> None:
        summary, detail = materialize_workout("unknown_non_gps_23")
        subdivisions = normalize_subdivisions(
            detail, summary=normalize_summary(summary)
        )

        selection = select_subdivisions(
            subdivisions,
            kinds=("pause", "rowing_interval"),
            max_records=1,
        )

        self.assertEqual(selection.matched_subdivisions, 2)
        self.assertEqual(len(selection.subdivisions), 1)
        self.assertTrue(selection.truncated)

    def test_detail_identity_mismatch_fails_without_echoing_values(self) -> None:
        summary, detail = materialize_workout("walking")
        detail["data"]["trackid"] = "sensitive-mismatched-id"

        with self.assertRaises(NormalizationError) as caught:
            normalize_workout(summary, detail)

        self.assertNotIn("sensitive-mismatched-id", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
