import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Config
from database import Database
from run_scheduled import main


class ScheduledRunnerTest(unittest.TestCase):
    def test_due_draft_is_claimed_by_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "scheduled.db"
            log_path = Path(tmp) / "scheduled.log"
            database = Database(db_path)
            database.init()
            item = {
                "id": "drive-due", "name": "due.jpg",
                "mimeType": "image/jpeg",
                "modifiedTime": "2026-09-12T00:00:00Z",
            }
            database.upsert_photo(item, "/tmp/due.jpg", "due-token")
            photo = database.list_photos("inbox")[0]
            draft_id = database.create_draft([photo["id"]], "caption")
            database.schedule_draft(
                draft_id, "caption", "2020-01-01T00:00:00+00:00"
            )

            with patch.object(Config, "DATABASE_PATH", db_path), patch.object(
                Config, "LOG_FILE", log_path
            ), patch.object(
                Config, "account_profiles", return_value={}
            ), patch(
                "run_scheduled.publish_claimed_draft",
                return_value={"media_id": "mock-media"},
            ) as publish:
                main()

            publish.assert_called_once()
            self.assertEqual(database.get_draft(draft_id)["status"], "publishing")


if __name__ == "__main__":
    unittest.main()
