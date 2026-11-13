import tempfile
import unittest
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from google.auth.exceptions import RefreshError
from database import Database
from drive_client import DriveClient


class DriveClientTest(unittest.TestCase):
    def test_revoked_google_token_reports_reauthorization_without_overwriting_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token.json"
            token_path.write_text("original", encoding="utf-8")
            credentials = MagicMock(expired=True, refresh_token="refresh-token")
            credentials.refresh.side_effect = RefreshError("invalid_grant")
            client = DriveClient(SimpleNamespace(GOOGLE_TOKEN_FILE=token_path))
            with patch("drive_client.Credentials.from_authorized_user_file", return_value=credentials):
                with self.assertRaisesRegex(RuntimeError, "create_token.py"):
                    client.credentials()
            self.assertEqual(token_path.read_text(encoding="utf-8"), "original")

    def test_google_token_saved_atomically_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "credentials" / "token.json"
            DriveClient._save_token(token_path, '{"new": true}')
            self.assertEqual(token_path.read_text(encoding="utf-8"), '{"new": true}')
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)

    def test_sync_reads_later_pages_when_earlier_files_are_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "test.db")
            database.init()
            registered = {"id": "existing", "name": "old.jpg", "mimeType": "image/jpeg"}
            database.upsert_photo(registered, "/tmp/old.jpg", "old-token")
            config = SimpleNamespace(DRIVE_INBOX_FOLDER_ID="inbox", MAX_SYNC_FILES=1,
                                     MEDIA_DIR=Path(tmp))
            client = DriveClient(config)
            service = MagicMock()
            service.files.return_value.list.return_value.execute.side_effect = [
                {"files": [registered], "nextPageToken": "next"},
                {"files": [{"id": "new", "name": "new.jpg", "mimeType": "image/jpeg"}]},
            ]
            with patch.object(client, "service", return_value=service), patch.object(
                client, "_download"
            ), patch("drive_client.prepare_instagram_image"):
                imported, skipped = client.sync(database)
            self.assertEqual((imported, skipped), (1, 1))
            self.assertEqual(len(database.list_photos()), 2)
            self.assertEqual(service.files.return_value.list.call_args_list[1].kwargs["pageToken"], "next")


if __name__ == "__main__":
    unittest.main()
