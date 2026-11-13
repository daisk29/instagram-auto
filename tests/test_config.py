import os
import unittest
from unittest.mock import patch

from config import Config


class ConfigTest(unittest.TestCase):
    def test_bisyu_account_profile(self):
        with patch.dict(os.environ, {
            "BISYU_DRIVE_INBOX_FOLDER_ID": "fixture_bisyu_inbox",
            "BISYU_DRIVE_POSTED_FOLDER_ID": "fixture_bisyu_posted",
        }):
            account = Config.account_profiles()["bisyu"]

        self.assertEqual(account.label, "美趣")
        self.assertEqual(account.username, "bisyu_1995")
        self.assertEqual(
            account.drive_inbox_folder_id,
            "fixture_bisyu_inbox",
        )
        self.assertEqual(
            account.drive_posted_folder_id,
            "fixture_bisyu_posted",
        )


if __name__ == "__main__":
    unittest.main()
