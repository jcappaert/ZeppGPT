import unittest

from zeppgpt.cli import _field_presence, _select_detail_candidates


class CliSelectionTests(unittest.TestCase):
    def test_detail_selection_prefers_distinct_sports(self) -> None:
        workouts = [
            {"trackid": "run-new", "source": "a", "type": 1, "sport_mode": 1},
            {"trackid": "run-old", "source": "a", "type": 1, "sport_mode": 1},
            {"trackid": "swim", "source": "b", "type": 17, "sport_mode": 6},
            {"trackid": "strength", "source": "c", "type": 64, "sport_mode": 24},
        ]

        selected = _select_detail_candidates(workouts, 3)

        self.assertEqual(
            [item["trackid"] for item in selected],
            ["run-new", "swim", "strength"],
        )

    def test_detail_selection_fills_remaining_slots(self) -> None:
        workouts = [
            {"trackid": "new", "source": "a", "type": 1},
            {"trackid": "old", "source": "a", "type": 1},
        ]

        selected = _select_detail_candidates(workouts, 2)

        self.assertEqual([item["trackid"] for item in selected], ["new", "old"])

    def test_field_presence_does_not_expose_values(self) -> None:
        presence = _field_presence(
            {
                "data": {
                    "trackid": 123,
                    "source": "source",
                    "heart_rate": "0,120;1,121",
                    "empty": "",
                    "version": 3,
                }
            }
        )

        self.assertEqual(
            presence,
            {
                "names": ["heart_rate", "version"],
                "large_value_lengths": {},
            },
        )


if __name__ == "__main__":
    unittest.main()
