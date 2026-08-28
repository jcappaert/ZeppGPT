import tempfile
import unittest
from pathlib import Path

from zeppgpt.config import ConfigError, ZeppConfig


class ZeppConfigTests(unittest.TestCase):
    def test_environment_overrides_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "ZEPP_APP_TOKEN=file-token\n"
                "ZEPP_USER_ID=123456\n"
                "ZEPP_API_HOST=https://api-mifit-de.huami.com\n",
                encoding="utf-8",
            )

            config = ZeppConfig.load(
                env_path,
                environ={"ZEPP_APP_TOKEN": "environment-token"},
            )

        self.assertEqual(config.app_token, "environment-token")
        self.assertEqual(config.user_id, "123456")
        self.assertEqual(config.api_hosts, ("https://api-mifit-de.huami.com",))

    def test_rejects_unapproved_token_destination(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unapproved host"):
            ZeppConfig.load(
                "/does/not/exist",
                environ={
                    "ZEPP_APP_TOKEN": "secret",
                    "ZEPP_USER_ID": "123",
                    "ZEPP_API_HOST": "https://example.com",
                },
            )

    def test_rejects_host_path_or_non_https_scheme(self) -> None:
        for host in (
            "http://api-mifit.zepp.com",
            "https://api-mifit.zepp.com/v1",
        ):
            with self.subTest(host=host), self.assertRaises(ConfigError):
                ZeppConfig.load(
                    "/does/not/exist",
                    environ={
                        "ZEPP_APP_TOKEN": "secret",
                        "ZEPP_USER_ID": "123",
                        "ZEPP_API_HOST": host,
                    },
                )

    def test_doctor_mode_allows_missing_credentials(self) -> None:
        config = ZeppConfig.load(
            "/does/not/exist",
            environ={},
            require_credentials=False,
        )
        self.assertEqual(config.app_token, "")
        self.assertEqual(config.user_id, "")
        self.assertTrue(config.api_hosts)

    def test_invalid_timeout_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "between 1 and 120"):
            ZeppConfig.load(
                "/does/not/exist",
                environ={
                    "ZEPP_APP_TOKEN": "secret",
                    "ZEPP_USER_ID": "123",
                    "ZEPP_REQUEST_TIMEOUT_SECONDS": "0",
                },
            )


if __name__ == "__main__":
    unittest.main()
