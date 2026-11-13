import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from database import Database
from publish_service import publish_claimed_draft, recover_stale_publishing


class PublishServiceTest(unittest.TestCase):
    @staticmethod
    def _make_publishing(database, suffix="stale"):
        item = {
            "id": f"drive-{suffix}", "name": f"{suffix}.jpg",
            "mimeType": "image/jpeg",
            "modifiedTime": "2026-09-12T00:00:00Z",
        }
        database.upsert_photo(
            item, f"/tmp/{suffix}.jpg", f"token-{suffix}", "crach29"
        )
        photo = database.list_photos("inbox", "crach29")[0]
        draft_id = database.create_draft([photo["id"]], "caption", "crach29")
        database.claim_publish(draft_id, "caption")
        with database.connect() as conn:
            conn.execute(
                "UPDATE drafts SET updated_at='2020-01-01T00:00:00+00:00' "
                "WHERE id=?",
                (draft_id,),
            )
        return draft_id, photo

    def test_claimed_draft_is_posted_and_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "test.db")
            database.init()
            item = {
                "id": "drive-publish", "name": "publish.jpg",
                "mimeType": "image/jpeg",
                "modifiedTime": "2026-09-12T00:00:00Z",
            }
            database.upsert_photo(
                item, "/tmp/publish.jpg", "publish-token", "crach29"
            )
            photo = database.list_photos("inbox", "crach29")[0]
            draft_id = database.create_draft(
                [photo["id"]], "caption", "crach29"
            )
            self.assertTrue(database.claim_publish(draft_id, "caption"))
            account = SimpleNamespace(key="crach29")

            with patch("publish_service.InstagramClient") as instagram_cls, patch(
                "publish_service.DriveClient"
            ) as drive_cls:
                instagram_cls.return_value.publish.return_value = (
                    "media-123", "https://instagram.example/post"
                )
                result = publish_claimed_draft(
                    database, object(), {"crach29": account}, draft_id
                )

            posted = database.get_draft(draft_id)
            self.assertEqual(posted["status"], "posted")
            self.assertEqual(posted["instagram_media_id"], "media-123")
            self.assertEqual(posted["progress"], 100)
            self.assertEqual(
                database.get_photos([photo["id"]])[0]["status"], "posted"
            )
            drive_cls.return_value.move_to_posted.assert_called_once_with(
                "drive-publish"
            )
            self.assertIsNone(result["warning"])

    def test_stale_publish_before_publication_becomes_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "test.db")
            database.init()
            draft_id, photo = self._make_publishing(database, "before")
            account = SimpleNamespace(key="crach29")
            config = SimpleNamespace(PUBLISH_STALE_MINUTES=10)

            recover_stale_publishing(
                database, config, {"crach29": account}
            )

            draft = database.get_draft(draft_id)
            self.assertEqual(draft["status"], "error")
            self.assertEqual(
                database.get_photos([photo["id"]])[0]["status"], "review"
            )

    def test_stale_publish_with_saved_media_id_is_finalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "test.db")
            database.init()
            draft_id, photo = self._make_publishing(database, "saved")
            database.update_draft(
                draft_id, instagram_media_id="media-saved", progress=80
            )
            with database.connect() as conn:
                conn.execute(
                    "UPDATE drafts SET updated_at='2020-01-01T00:00:00+00:00' "
                    "WHERE id=?",
                    (draft_id,),
                )
            account = SimpleNamespace(key="crach29")
            config = SimpleNamespace(PUBLISH_STALE_MINUTES=10)

            with patch("publish_service.DriveClient") as drive_cls:
                recover_stale_publishing(
                    database, config, {"crach29": account}
                )

            self.assertEqual(database.get_draft(draft_id)["status"], "posted")
            self.assertEqual(
                database.get_photos([photo["id"]])[0]["status"], "posted"
            )
            drive_cls.return_value.move_to_posted.assert_called_once()

    def test_ambiguous_stale_publish_is_reconciled_with_instagram(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "test.db")
            database.init()
            draft_id, _ = self._make_publishing(database, "ambiguous")
            database.update_draft(draft_id, progress=70)
            with database.connect() as conn:
                conn.execute(
                    "UPDATE drafts SET updated_at='2020-01-01T00:00:00+00:00' "
                    "WHERE id=?",
                    (draft_id,),
                )
            account = SimpleNamespace(key="crach29")
            config = SimpleNamespace(PUBLISH_STALE_MINUTES=10)

            with patch("publish_service.InstagramClient") as instagram_cls, patch(
                "publish_service.DriveClient"
            ):
                instagram_cls.return_value.find_recent_post.return_value = {
                    "id": "media-recovered",
                    "permalink": "https://instagram.example/recovered",
                }
                recover_stale_publishing(
                    database, config, {"crach29": account}
                )

            draft = database.get_draft(draft_id)
            self.assertEqual(draft["status"], "posted")
            self.assertEqual(draft["instagram_media_id"], "media-recovered")

    def test_communication_loss_after_publish_start_waits_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "test.db")
            database.init()
            draft_id, photo = self._make_publishing(database, "uncertain")
            account = SimpleNamespace(key="crach29")

            def interrupted(*args, **kwargs):
                kwargs["on_publish_start"]()
                raise RuntimeError("connection lost")

            with patch("publish_service.InstagramClient") as client:
                client.return_value.publish.side_effect = interrupted
                with self.assertRaisesRegex(RuntimeError, "connection lost"):
                    publish_claimed_draft(database, object(), {"crach29": account}, draft_id)

            draft = database.get_draft(draft_id)
            self.assertEqual(draft["status"], "committing")
            self.assertIn("確認中", draft["progress_message"])
            self.assertEqual(database.get_photos([photo["id"]])[0]["status"], "review")

    def test_unmatched_publish_result_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "test.db")
            database.init()
            draft_id, photo = self._make_publishing(database, "unverified")
            self.assertTrue(database.begin_instagram_publish(draft_id))
            with database.connect() as conn:
                conn.execute("UPDATE drafts SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (draft_id,))
            account = SimpleNamespace(key="crach29")
            with patch("publish_service.InstagramClient") as client:
                client.return_value.find_recent_post.return_value = None
                recover_stale_publishing(
                    database, SimpleNamespace(PUBLISH_STALE_MINUTES=10),
                    {"crach29": account},
                )
            self.assertEqual(database.get_draft(draft_id)["status"], "unverified")
            self.assertFalse(database.claim_publish(draft_id, "unsafe retry"))
            self.assertTrue(database.confirm_unpublished(draft_id))
            self.assertFalse(database.confirm_unpublished(draft_id))
            self.assertTrue(database.claim_publish(draft_id, "safe retry"))
            self.assertEqual(database.get_photos([photo["id"]])[0]["status"], "review")

    def test_cancel_during_upload_does_not_turn_into_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "test.db")
            database.init()
            draft_id, photo = self._make_publishing(database, "cancel-race")
            account = SimpleNamespace(key="crach29")

            def cancelled(*args, **kwargs):
                self.assertTrue(database.cancel_publish(draft_id))
                kwargs["on_progress"](35, "late progress")

            with patch("publish_service.InstagramClient") as client:
                client.return_value.publish.side_effect = cancelled
                result = publish_claimed_draft(database, object(), {"crach29": account}, draft_id)

            self.assertTrue(result["cancelled"])
            self.assertEqual(database.get_draft(draft_id)["status"], "cancelled")
            self.assertEqual(database.get_photos([photo["id"]])[0]["status"], "review")


if __name__ == "__main__":
    unittest.main()
