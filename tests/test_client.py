import unittest
from datetime import date

from zeppgpt.client import (
    DETAIL_PATH,
    HISTORY_PATH,
    ApiResponse,
    ZeppApiClient,
    ZeppApiError,
    compact_workout,
)
from zeppgpt.config import ZeppConfig


def history_page(summary, next_id=None):
    return {
        "code": 1,
        "message": "success",
        "data": {"summary": summary, "next": next_id},
    }


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get_json(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ApiResponse(status=200, payload=response)


class HostTransport:
    def __init__(self, successful_host, payload):
        self.successful_host = successful_host
        self.payload = payload
        self.calls = []

    def get_json(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["host"] != self.successful_host:
            raise ZeppApiError("not found", status=404)
        return ApiResponse(status=200, payload=self.payload)


class ZeppApiClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ZeppConfig(
            app_token="top-secret-token",
            user_id="987654",
            api_hosts=("https://api-mifit.zepp.com",),
        )

    def test_host_probe_selects_first_success_and_uses_auth_header(self) -> None:
        config = ZeppConfig(
            app_token="top-secret-token",
            user_id="987654",
            api_hosts=(
                "https://api-mifit-de.huami.com",
                "https://api-mifit.zepp.com",
            ),
        )
        payload = history_page([])
        transport = HostTransport("https://api-mifit.zepp.com", payload)
        client = ZeppApiClient(config, transport=transport)

        host, returned = client.probe_hosts()

        self.assertEqual(host, "https://api-mifit.zepp.com")
        self.assertEqual(returned, payload)
        self.assertEqual(transport.calls[-1]["headers"]["apptoken"], "top-secret-token")
        self.assertEqual(transport.calls[-1]["path"], HISTORY_PATH)
        self.assertEqual(transport.calls[-1]["params"]["userid"], "987654")

    def test_history_pagination_uses_next_cursor_and_deduplicates(self) -> None:
        first = history_page(
            [
                {
                    "trackid": "300",
                    "source": "run.watch.huami.com",
                    "type": 1,
                    "end_time": 1_704_153_600,
                    "run_time": 3600,
                }
            ],
            next_id="250",
        )
        second = history_page(
            [
                {
                    "trackid": "300",
                    "source": "run.watch.huami.com",
                    "type": 1,
                    "end_time": 1_704_153_600,
                    "run_time": 3600,
                },
                {
                    "trackid": "200",
                    "source": "run.mifit.huami.com",
                    "type": 17,
                    "end_time": 1_704_067_200,
                    "run_time": 1800,
                },
            ]
        )
        transport = QueueTransport([second])
        client = ZeppApiClient(self.config, transport=transport)
        client.active_host = self.config.api_hosts[0]

        workouts, pages = client.list_workouts(
            limit=10,
            max_pages=3,
            first_page=first,
        )

        self.assertEqual([item["trackid"] for item in workouts], ["300", "200"])
        self.assertEqual(len(pages), 2)
        self.assertEqual(transport.calls[0]["params"]["stopTrackId"], "250")

    def test_history_filters_by_utc_start_date(self) -> None:
        first = history_page(
            [
                {
                    "trackid": "new",
                    "end_time": 1_704_153_600,
                    "run_time": 0,
                },
                {
                    "trackid": "old",
                    "end_time": 1_703_980_800,
                    "run_time": 0,
                },
            ]
        )
        transport = QueueTransport([])
        client = ZeppApiClient(self.config, transport=transport)
        client.active_host = self.config.api_hosts[0]

        workouts, _ = client.list_workouts(
            limit=10,
            max_pages=1,
            start_date=date(2024, 1, 2),
            first_page=first,
        )

        self.assertEqual([item["trackid"] for item in workouts], ["new"])

    def test_start_date_prefers_timestamp_track_id_for_paused_workout(self) -> None:
        compact = compact_workout(
            {
                "trackid": "1704067200",
                "source": "source",
                "end_time": "1704153600",
                "run_time": "60",
                "pause_time": "86340",
            }
        )

        self.assertEqual(compact["date_utc"], "2024-01-01")

    def test_detail_requires_and_sends_track_id_plus_source(self) -> None:
        payload = {"code": 1, "data": {"trackid": "300", "lap": "1,2"}}
        transport = QueueTransport([payload])
        client = ZeppApiClient(self.config, transport=transport)
        client.active_host = self.config.api_hosts[0]

        returned = client.get_workout_detail(
            track_id="300", source="run.watch.huami.com"
        )

        self.assertEqual(returned, payload)
        self.assertEqual(transport.calls[0]["path"], DETAIL_PATH)
        self.assertEqual(
            transport.calls[0]["params"],
            {"trackid": "300", "source": "run.watch.huami.com"},
        )

    def test_upstream_error_does_not_echo_message_or_secret(self) -> None:
        payload = {
            "code": -1,
            "message": "request contained top-secret-token",
            "data": {},
        }
        transport = QueueTransport([payload])
        client = ZeppApiClient(self.config, transport=transport)

        with self.assertRaises(ZeppApiError) as caught:
            client.probe_hosts()

        message = str(caught.exception)
        self.assertNotIn("top-secret-token", message)
        self.assertNotIn("request contained", message)

    def test_compact_workout_preserves_unknown_type_id(self) -> None:
        compact = compact_workout(
            {
                "trackid": "abc",
                "source": "source",
                "type": 9999,
                "end_time": 1_704_153_600,
                "run_time": "60",
                "dis": "123.4",
            }
        )
        self.assertEqual(compact["type_id"], 9999)
        self.assertEqual(compact["distance_meters"], 123.4)


if __name__ == "__main__":
    unittest.main()
