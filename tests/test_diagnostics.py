import json
import tempfile
import unittest
from pathlib import Path

from zeppgpt.diagnostics import field_inventory, redact, write_json


class DiagnosticsTests(unittest.TestCase):
    def test_redacts_secret_keys_and_secret_values(self) -> None:
        payload = {
            "appToken": "camel-secret",
            "app_token": "snake-secret",
            "nested": {
                "authorization": "Bearer credential",
                "message": "echo exact-token here",
            },
        }

        sanitized = redact(payload, secrets=("exact-token",))

        self.assertEqual(sanitized["app_token"], "<redacted>")
        self.assertEqual(sanitized["nested"]["authorization"], "<redacted>")
        self.assertEqual(sanitized["nested"]["message"], "echo <redacted> here")
        # camelCase key support is intentionally verified too.
        self.assertEqual(sanitized["appToken"], "<redacted>")

    def test_inventory_reports_shapes_without_values(self) -> None:
        payload = {
            "data": {
                "heart_rate": "0,120;1,121",
                "laps": [{"distance": 100}, {"distance": 200}],
            }
        }

        inventory = field_inventory(payload)
        serialized = json.dumps(inventory)

        self.assertIn("data.heart_rate", serialized)
        self.assertIn("data.laps[].distance", serialized)
        self.assertNotIn("0,120", serialized)
        self.assertNotIn("200", serialized)

    def test_written_json_never_contains_exact_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.json"
            write_json(
                path,
                {"message": "token=exact-secret", "cookie": "value"},
                secrets=("exact-secret",),
            )
            content = path.read_text(encoding="utf-8")

        self.assertNotIn("exact-secret", content)
        self.assertNotIn('"cookie": "value"', content)


if __name__ == "__main__":
    unittest.main()
