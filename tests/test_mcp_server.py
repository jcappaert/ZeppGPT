import contextlib
import io
import unittest

from mcp.client import Client
from test_service import FakeWorkoutApi

from zeppgpt.mcp_server import (
    MAX_MCP_SAMPLE_POINTS,
    create_mcp_server,
    main,
)
from zeppgpt.service import ZeppWorkoutService


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.api = FakeWorkoutApi()
        self.service = ZeppWorkoutService(self.api)
        self.server = create_mcp_server(self.service)

    async def test_protocol_lists_five_structured_read_only_tools(self):
        async with Client(self.server) as client:
            result = await client.list_tools()

        self.assertEqual(
            [tool.name for tool in result.tools],
            [
                "list_workouts",
                "get_workout",
                "get_latest_workout",
                "get_workout_samples",
                "get_workout_laps",
            ],
        )
        for tool in result.tools:
            self.assertIsNotNone(tool.output_schema)
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertFalse(tool.annotations.open_world_hint)
            self.assertTrue(tool.description.startswith("Use this when"))

        samples_tool = next(
            tool for tool in result.tools if tool.name == "get_workout_samples"
        )
        self.assertEqual(
            samples_tool.input_schema["properties"]["max_points"]["maximum"],
            MAX_MCP_SAMPLE_POINTS,
        )

    async def test_all_tools_return_valid_structured_content(self):
        async with Client(self.server) as client:
            listed = await client.call_tool("list_workouts", {"limit": 3})
            self.assertFalse(listed.is_error)
            self.assertEqual(listed.structured_content["count"], 3)
            workout_id = listed.structured_content["workouts"][0]["workout_id"]

            workout = await client.call_tool(
                "get_workout",
                {"workout_id": workout_id, "include_raw_metadata": False},
            )
            latest = await client.call_tool("get_latest_workout", {})
            samples = await client.call_tool(
                "get_workout_samples",
                {
                    "workout_id": workout_id,
                    "fields": ["heart_rate_bpm", "distance_meters"],
                    "max_points": 2,
                },
            )
            laps = await client.call_tool(
                "get_workout_laps",
                {"workout_id": workout_id, "max_records": 2},
            )

        for result in (workout, latest, samples, laps):
            self.assertFalse(result.is_error)
            self.assertIsNotNone(result.structured_content)
            self.assertTrue(result.content)
        self.assertLessEqual(samples.structured_content["returned_samples"], 2)
        self.assertLessEqual(laps.structured_content["returned_subdivisions"], 2)

    async def test_invalid_workout_id_error_does_not_echo_input(self):
        sensitive_marker = "sensitive-marker-that-must-not-echo"
        async with Client(self.server) as client:
            result = await client.call_tool(
                "get_workout",
                {"workout_id": sensitive_marker},
            )

        self.assertTrue(result.is_error)
        serialized = result.model_dump_json()
        self.assertNotIn(sensitive_marker, serialized)
        self.assertIn("invalid Zepp workout ID", serialized)

    def test_refuses_unauthenticated_non_local_http_bind(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(["--host", "0.0.0.0"])

        self.assertEqual(exit_code, 2)
        self.assertIn("Refusing unauthenticated non-local bind", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
