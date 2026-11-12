from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.config import ConfigError, load_telegram_config


class LoadTelegramConfigTests(unittest.TestCase):
    def test_missing_credentials(self) -> None:
        with patch("src.config.load_dotenv"), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load_telegram_config()
        message = str(ctx.exception)
        self.assertIn("TELEGRAM_API_ID", message)
        self.assertIn("TELEGRAM_API_HASH", message)
        self.assertNotIn("0123456789abcdef", message)

    def test_invalid_api_id(self) -> None:
        env = {
            "TELEGRAM_API_ID": "not-a-number",
            "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
        }
        with patch("src.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load_telegram_config()
        self.assertIn("whole number", str(ctx.exception))

    def test_invalid_api_hash(self) -> None:
        env = {"TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "short"}
        with patch("src.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load_telegram_config()
        self.assertIn("32-character hexadecimal", str(ctx.exception))

    def test_valid_credentials(self) -> None:
        env = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
        }
        with patch("src.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
            config = load_telegram_config()
        self.assertEqual(config.api_id, 12345)
        self.assertEqual(config.api_hash, "0123456789abcdef0123456789abcdef")


if __name__ == "__main__":
    unittest.main()
