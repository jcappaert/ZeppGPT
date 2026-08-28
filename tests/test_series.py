import json
import unittest
from pathlib import Path

from zeppgpt.series import (
    SeriesDecodeError,
    decode_change_series,
    decode_route,
    require_record_width,
    split_records,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "zepp_detail_series.json"


class ZeppSeriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_split_records_preserves_interior_empty_route_point(self) -> None:
        records = split_records(self.fixture["longitude_latitude"])

        self.assertEqual(len(records), 4)
        self.assertEqual(records[2], ("",))

    def test_decode_delta_cumulative_heart_rate(self) -> None:
        series = decode_change_series(
            self.fixture["heart_rate"],
            field="heart_rate",
            cumulative_columns=frozenset({0}),
        )

        self.assertEqual(
            [(point.offset_seconds, point.values) for point in series.points],
            [(0, (100,)), (3, (105,)), (4, (102,))],
        )
        self.assertEqual(series.span_seconds, 6)

    def test_decode_fixed_speed(self) -> None:
        series = decode_change_series(self.fixture["speed"], field="speed")

        self.assertEqual(
            [(point.offset_seconds, point.values) for point in series.points],
            [(0, (1.5,)), (3, (2.0,))],
        )
        self.assertEqual(series.span_seconds, 4)

    def test_decode_fixed_current_distance_centimeters(self) -> None:
        series = decode_change_series(
            self.fixture["currentDistance"],
            field="currentDistance",
        )

        self.assertEqual(
            [(point.offset_seconds, point.values) for point in series.points],
            [(0, (0,)), (3, (500,)), (4, (1000,))],
        )

    def test_decode_mixed_gait_values(self) -> None:
        series = decode_change_series(
            self.fixture["gait"],
            field="gait",
            value_columns=3,
            cumulative_columns=frozenset({0}),
        )

        self.assertEqual(series.points[0].values, (1, 80, 160))
        self.assertEqual(series.points[1].values, (3, 82, 162))

    def test_decode_route_with_missing_position_and_altitude(self) -> None:
        route = decode_route(self.fixture)

        self.assertEqual([point.offset_seconds for point in route], [0, 1, 2, 4])
        self.assertAlmostEqual(route[0].latitude_degrees or 0, 10.0)
        self.assertAlmostEqual(route[0].longitude_degrees or 0, 20.0)
        self.assertAlmostEqual(route[1].latitude_degrees or 0, 10.000001)
        self.assertAlmostEqual(route[1].longitude_degrees or 0, 19.999999)
        self.assertIsNone(route[2].latitude_degrees)
        self.assertIsNone(route[2].longitude_degrees)
        self.assertIsNone(route[2].altitude_meters)
        self.assertAlmostEqual(route[3].latitude_degrees or 0, 10.000003)
        self.assertAlmostEqual(route[3].longitude_degrees or 0, 20.000002)
        self.assertEqual(route[0].altitude_meters, 100.0)
        self.assertEqual(route[3].flag, 2)

    def test_wide_lap_record_can_be_shape_checked_without_guessing_columns(self) -> None:
        lap = ["0"] * 70
        lap[3] = "synthetic-label"
        records = split_records(",".join(lap) + ";")

        require_record_width(records, expected_columns=70, field="lap")
        self.assertEqual(records[0][3], "synthetic-label")

    def test_shape_and_value_errors_do_not_echo_payload_values(self) -> None:
        secret_marker = "sensitive-value-123"
        with self.assertRaises(SeriesDecodeError) as shape_error:
            decode_change_series(f"1,2,{secret_marker};", field="heart_rate")
        with self.assertRaises(SeriesDecodeError) as value_error:
            decode_change_series(f"1,{secret_marker};", field="heart_rate")

        self.assertNotIn(secret_marker, str(shape_error.exception))
        self.assertNotIn(secret_marker, str(value_error.exception))


if __name__ == "__main__":
    unittest.main()
