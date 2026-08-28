import json
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path

from zeppgpt.client import workout_start_date
from zeppgpt.models import WorkoutId
from zeppgpt.service import WorkoutNotFoundError, ZeppWorkoutService

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "zepp_workout_shapes.json"


def fixture_records():
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    summaries = []
    details = {}
    for record in fixture["workouts"]:
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
        identity = WorkoutId(track_id, summary["source"])
        summaries.append(summary)
        details[identity] = {"code": 1, "data": detail}
    return summaries, details


class FakeWorkoutApi:
    def __init__(self):
        self.summaries, self.details = fixture_records()
        self.active_host = None
        self.probe_calls = 0
        self.history_calls = 0
        self.detail_calls = 0

    def probe_hosts(self):
        self.probe_calls += 1
        self.active_host = "https://api-mifit.zepp.com"
        return self.active_host, {
            "code": 1,
            "data": {"summary": self.summaries, "next": None},
        }

    def list_workouts(
        self,
        *,
        limit,
        max_pages,
        start_date=None,
        end_date=None,
        first_page=None,
    ):
        self.history_calls += 1
        items = []
        for summary in self.summaries:
            started = workout_start_date(summary)
            if start_date and started and started < start_date:
                continue
            if end_date and started and started > end_date:
                continue
            items.append(summary)
        return items[:limit], []

    def get_workout_detail(self, *, track_id, source):
        self.detail_calls += 1
        return self.details[WorkoutId(track_id, source)]


class WorkoutServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeWorkoutApi()
        self.service = ZeppWorkoutService(self.api)

    def test_list_is_newest_first_and_supports_known_or_numeric_sport_filter(self):
        all_workouts = self.service.list_workouts(limit=6)
        running = self.service.list_workouts(sport_type="running", limit=6)
        unknown_22 = self.service.list_workouts(sport_type="22", limit=6)

        self.assertEqual(len(all_workouts), 6)
        self.assertGreater(
            all_workouts[0].start_time_utc,
            all_workouts[-1].start_time_utc,
        )
        self.assertEqual([item.workout_type.name for item in running], ["running"])
        self.assertEqual(
            [item.workout_type.zepp_type_id for item in unknown_22], [22]
        )
        self.assertEqual(self.api.probe_calls, 1)

    def test_date_filter_is_passed_to_bounded_history_scan(self):
        workouts = self.service.list_workouts(
            start_date=date(2023, 11, 19),
            limit=100,
        )

        self.assertTrue(workouts)
        self.assertTrue(
            all(item.start_time_utc.date() >= date(2023, 11, 19) for item in workouts)
        )

    def test_detail_samples_and_subdivisions_reuse_detail_cache(self):
        summary = self.service.list_workouts(sport_type="23", limit=1)[0]

        workout = self.service.get_workout(summary.workout_id)
        samples, available_fields = self.service.get_workout_samples(
            summary.workout_id,
            require_fields=("heart_rate_bpm",),
            max_points=2,
        )
        subdivisions, available_kinds = self.service.get_workout_subdivisions(
            summary.workout_id,
            max_records=2,
        )

        self.assertEqual(workout.summary.workout_type.zepp_type_id, 23)
        self.assertLessEqual(len(samples.samples), 2)
        self.assertIn("heart_rate_bpm", available_fields)
        self.assertLessEqual(len(subdivisions.subdivisions), 2)
        self.assertIn("rowing_interval", available_kinds)
        self.assertEqual(self.api.detail_calls, 1)

    def test_latest_workout_can_filter_hiking_category(self):
        latest = self.service.get_latest_workout("hiking")

        self.assertTrue(latest.summary.workout_type.known)
        self.assertEqual(latest.summary.workout_type.zepp_type_id, 22)

    def test_invalid_and_unseen_workout_ids_fail_safely(self):
        with self.assertRaisesRegex(ValueError, "invalid Zepp workout ID"):
            self.service.get_workout("not-valid")

        unseen = WorkoutId("1999999999", "synthetic.watch")
        with self.assertRaisesRegex(WorkoutNotFoundError, "not found"):
            self.service.get_workout(unseen)


if __name__ == "__main__":
    unittest.main()
