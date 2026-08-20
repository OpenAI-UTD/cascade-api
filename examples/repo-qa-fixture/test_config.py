import unittest

from app_config import RETRY_TIMEOUT_SECONDS


class ConfigTest(unittest.TestCase):
    def test_retry_timeout(self) -> None:
        self.assertEqual(RETRY_TIMEOUT_SECONDS, 30, "timeout must be 30 seconds")


if __name__ == "__main__":
    unittest.main()
