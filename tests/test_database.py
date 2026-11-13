import json
import tempfile
import unittest
from pathlib import Path

from database import Database


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.db.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_photo_draft_and_publish_claim(self):
        item = {"id": "drive-1", "name": "one.jpg", "mimeType": "image/jpeg", "modifiedTime": "2026-09-12T00:00:00Z"}
        self.db.upsert_photo(item, "/tmp/one.jpg", "token-one")
        photo = self.db.list_photos("inbox")[0]
        draft_id = self.db.create_draft([photo["id"]], "caption")
        draft = self.db.get_draft(draft_id)
        self.assertEqual(json.loads(draft["photo_ids"]), [photo["id"]])
        self.assertTrue(self.db.claim_publish(draft_id, "final caption"))
        self.assertFalse(self.db.claim_publish(draft_id, "duplicate"))
        publishing = self.db.get_draft(draft_id)
        self.assertEqual(publishing["status"], "publishing")
        self.assertEqual(publishing["progress"], 5)
        self.assertEqual(photo["account_key"], "29shashinten")
        self.assertEqual(draft["account_key"], "29shashinten")

    def test_draft_creation_rechecks_photos_and_rejects_duplicates(self):
        item = {"id": "drive-reuse", "name": "reuse.jpg", "mimeType": "image/jpeg"}
        self.db.upsert_photo(item, "/tmp/reuse.jpg", "token-reuse")
        photo_id = self.db.list_photos("inbox")[0]["id"]
        with self.assertRaises(ValueError):
            self.db.create_draft([photo_id, photo_id], "duplicate")
        with self.assertRaises(ValueError):
            self.db.create_draft([photo_id], "wrong account", "bisyu")
        self.db.create_draft([photo_id], "first")
        with self.assertRaises(ValueError):
            self.db.create_draft([photo_id], "stale selection")
        self.assertEqual(len(self.db.list_drafts()), 1)

    def test_editing_cannot_overwrite_scheduled_or_published_state(self):
        item = {"id": "drive-edit", "name": "edit.jpg", "mimeType": "image/jpeg"}
        self.db.upsert_photo(item, "/tmp/edit.jpg", "token-edit")
        photo_id = self.db.list_photos("inbox")[0]["id"]
        draft_id = self.db.create_draft([photo_id], "original")
        self.assertTrue(self.db.update_editable_draft(draft_id, "held", "hold"))
        self.assertEqual(self.db.get_photos([photo_id])[0]["status"], "hold")
        self.assertTrue(self.db.schedule_draft(draft_id, "scheduled", "2026-12-01T00:00:00+00:00"))
        self.assertFalse(self.db.schedule_draft(draft_id, "changed", "2026-12-02T00:00:00+00:00"))
        self.assertFalse(self.db.update_editable_draft(draft_id, "forged", "hold"))
        self.assertEqual(self.db.get_draft(draft_id)["caption"], "scheduled")
        self.assertEqual(self.db.get_photos([photo_id])[0]["status"], "scheduled")
        self.assertTrue(self.db.cancel_schedule(draft_id))
        self.assertEqual(self.db.get_photos([photo_id])[0]["status"], "review")
        self.assertTrue(self.db.claim_publish(draft_id, "publishing"))
        self.assertFalse(self.db.update_editable_draft(draft_id, "forged", "draft"))
        self.assertTrue(self.db.begin_instagram_publish(draft_id))
        self.assertTrue(self.db.finish_publish(draft_id, "media-id", ""))
        self.assertFalse(self.db.fail_publish(draft_id, "late error"))
        self.assertEqual(self.db.get_draft(draft_id)["status"], "posted")
        self.assertEqual(self.db.get_photos([photo_id])[0]["status"], "posted")

    def test_cancelled_publish_cannot_be_overwritten_by_late_progress(self):
        item = {"id": "drive-late", "name": "late.jpg", "mimeType": "image/jpeg"}
        self.db.upsert_photo(item, "/tmp/late.jpg", "token-late")
        photo_id = self.db.list_photos("inbox")[0]["id"]
        draft_id = self.db.create_draft([photo_id], "original")
        self.db.claim_publish(draft_id, "caption")
        self.assertTrue(self.db.cancel_publish(draft_id))
        self.assertFalse(self.db.update_publish_progress(draft_id, progress=75))
        self.assertFalse(self.db.fail_publish(draft_id, "late failure"))
        self.assertEqual(self.db.get_draft(draft_id)["status"], "cancelled")
        self.assertEqual(self.db.get_photos([photo_id])[0]["status"], "review")

    def test_accounts_are_filtered_independently(self):
        first = {
            "id": "drive-primary", "name": "primary.jpg",
            "mimeType": "image/jpeg", "modifiedTime": "2026-09-12T00:00:00Z",
        }
        second = {
            "id": "drive-crach", "name": "crach.jpg",
            "mimeType": "image/jpeg", "modifiedTime": "2026-09-12T00:01:00Z",
        }
        self.db.upsert_photo(first, "/tmp/primary.jpg", "token-primary")
        self.db.upsert_photo(
            second, "/tmp/crach.jpg", "token-crach", "crach29"
        )
        primary = self.db.list_photos("inbox", "29shashinten")
        crach = self.db.list_photos("inbox", "crach29")
        self.assertEqual([row["name"] for row in primary], ["primary.jpg"])
        self.assertEqual([row["name"] for row in crach], ["crach.jpg"])

        draft_id = self.db.create_draft([crach[0]["id"]], "caption", "crach29")
        self.assertEqual(self.db.get_draft(draft_id)["account_key"], "crach29")
        self.assertEqual(len(self.db.list_drafts("29shashinten")), 0)
        self.assertEqual(len(self.db.list_drafts("crach29")), 1)

    def test_schedule_cancel_and_atomic_claim(self):
        item = {
            "id": "drive-scheduled", "name": "scheduled.jpg",
            "mimeType": "image/jpeg", "modifiedTime": "2026-09-12T00:00:00Z",
        }
        self.db.upsert_photo(item, "/tmp/scheduled.jpg", "token-scheduled")
        photo = self.db.list_photos("inbox")[0]
        draft_id = self.db.create_draft([photo["id"]], "caption")

        scheduled_at = "2026-09-13T07:00:00+00:00"
        self.assertTrue(self.db.schedule_draft(draft_id, "final", scheduled_at))
        scheduled = self.db.get_draft(draft_id)
        self.assertEqual(scheduled["status"], "scheduled")
        self.assertEqual(scheduled["scheduled_at"], scheduled_at)
        self.assertEqual(
            len(self.db.list_due_drafts("2026-09-13T06:59:59+00:00")), 0
        )
        self.assertTrue(self.db.cancel_schedule(draft_id))
        self.assertEqual(self.db.get_draft(draft_id)["status"], "draft")

        self.assertTrue(self.db.schedule_draft(draft_id, "final", scheduled_at))
        self.assertTrue(
            self.db.claim_scheduled_publish(
                draft_id, "2026-09-13T07:00:00+00:00"
            )
        )
        self.assertFalse(
            self.db.claim_scheduled_publish(
                draft_id, "2026-09-13T07:00:00+00:00"
            )
        )
        self.assertEqual(self.db.get_draft(draft_id)["status"], "publishing")

    def test_publish_can_be_cancelled_only_before_commit(self):
        item = {
            "id": "drive-cancel", "name": "cancel.jpg",
            "mimeType": "image/jpeg", "modifiedTime": "2026-09-12T00:00:00Z",
        }
        self.db.upsert_photo(item, "/tmp/cancel.jpg", "token-cancel")
        photo = self.db.list_photos("inbox")[0]
        draft_id = self.db.create_draft([photo["id"]], "caption")

        self.assertTrue(self.db.claim_publish(draft_id, "caption"))
        self.assertTrue(self.db.cancel_publish(draft_id))
        self.assertEqual(self.db.get_draft(draft_id)["status"], "cancelled")
        self.assertTrue(self.db.claim_publish(draft_id, "caption"))
        self.assertTrue(self.db.begin_instagram_publish(draft_id))
        self.assertEqual(self.db.get_draft(draft_id)["status"], "committing")
        self.assertFalse(self.db.cancel_publish(draft_id))

    def test_only_held_or_cancelled_draft_can_be_deleted(self):
        item = {
            "id": "drive-delete", "name": "delete.jpg",
            "mimeType": "image/jpeg", "modifiedTime": "2026-09-12T00:00:00Z",
        }
        self.db.upsert_photo(item, "/tmp/delete.jpg", "token-delete")
        photo = self.db.list_photos("inbox")[0]
        draft_id = self.db.create_draft([photo["id"]], "caption")

        self.assertFalse(self.db.delete_inactive_draft(draft_id))
        self.db.update_draft(draft_id, status="hold")
        self.db.event(draft_id, "held")
        self.assertTrue(self.db.delete_inactive_draft(draft_id))
        self.assertIsNone(self.db.get_draft(draft_id))
        self.assertEqual(self.db.get_photos([photo["id"]])[0]["status"], "inbox")
        with self.db.connect() as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE draft_id=?", (draft_id,)
            ).fetchone()[0]
        self.assertEqual(event_count, 0)

    def test_cancelled_draft_can_be_deleted(self):
        item = {
            "id": "drive-delete-cancelled", "name": "cancelled.jpg",
            "mimeType": "image/jpeg", "modifiedTime": "2026-09-12T00:00:00Z",
        }
        self.db.upsert_photo(
            item, "/tmp/cancelled.jpg", "token-delete-cancelled"
        )
        photo = self.db.list_photos("inbox")[0]
        draft_id = self.db.create_draft([photo["id"]], "caption")
        self.assertTrue(self.db.claim_publish(draft_id, "caption"))
        self.assertTrue(self.db.cancel_publish(draft_id))
        self.assertTrue(self.db.delete_inactive_draft(draft_id))
        self.assertIsNone(self.db.get_draft(draft_id))

    def test_existing_database_gets_progress_and_account_columns(self):
        with self.db.connect() as conn:
            conn.execute("DROP TABLE events")
            conn.execute("DROP TABLE drafts")
            conn.execute("DROP TABLE photos")
            conn.execute(
                """
                CREATE TABLE photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drive_file_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    modified_time TEXT,
                    local_path TEXT NOT NULL,
                    public_token TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'inbox',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    photo_ids TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    instagram_media_id TEXT,
                    permalink TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    posted_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO photos
                    (drive_file_id, name, mime_type, local_path, public_token,
                     status, created_at, updated_at)
                VALUES ('legacy-drive', 'legacy.jpg', 'image/jpeg',
                        '/tmp/legacy.jpg', 'legacy-token', 'posted', 'now', 'now')
                """
            )
            conn.execute(
                """
                INSERT INTO drafts(photo_ids, caption, status, created_at, updated_at)
                VALUES ('[1]', 'legacy caption', 'posted', 'now', 'now')
                """
            )

        self.db.init()
        with self.db.connect() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(drafts)")
            }
        self.assertIn("progress", columns)
        self.assertIn("progress_message", columns)
        self.assertIn("account_key", columns)
        self.assertIn("scheduled_at", columns)
        with self.db.connect() as conn:
            photo_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(photos)")
            }
        self.assertIn("account_key", photo_columns)
        self.assertEqual(
            self.db.list_photos()[0]["account_key"], "29shashinten"
        )
        self.assertEqual(
            self.db.list_drafts()[0]["account_key"], "29shashinten"
        )


if __name__ == "__main__":
    unittest.main()
