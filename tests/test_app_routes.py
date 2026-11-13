import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from database import Database


class DraftRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "test.db")
        self.database.init()
        replacement = patch.object(app_module, "db", self.database)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["csrf_token"] = "test-csrf"
        self.database.upsert_photo(
            {"id": "drive-route", "name": "photo.jpg", "mimeType": "image/jpeg"},
            "/tmp/photo.jpg", "route-token",
        )
        self.photo_id = self.database.list_photos("inbox")[0]["id"]
        self.draft_id = self.database.create_draft([self.photo_id], "original")

    def post(self, action, **data):
        return self.client.post(
            f"/drafts/{self.draft_id}/{action}",
            data={"csrf_token": "test-csrf", **data},
        )

    def test_stale_form_cannot_edit_publishing_or_posted_draft(self):
        self.database.claim_publish(self.draft_id, "publishing")
        for action in ("save", "hold", "regenerate"):
            with patch("app.CaptionClient") as caption:
                self.assertEqual(self.post(action, caption="forged").status_code, 302)
                caption.assert_not_called()
            self.assertEqual(self.database.get_draft(self.draft_id)["status"], "publishing")
            self.assertEqual(self.database.get_draft(self.draft_id)["caption"], "publishing")
        self.database.begin_instagram_publish(self.draft_id)
        self.database.finish_publish(self.draft_id, "media-id", "")
        self.post("hold", caption="forged after post")
        self.assertEqual(self.database.get_draft(self.draft_id)["status"], "posted")
        self.assertEqual(self.database.get_photos([self.photo_id])[0]["status"], "posted")

    def test_unverified_requires_form_confirmation(self):
        self.database.claim_publish(self.draft_id, "caption")
        self.database.begin_instagram_publish(self.draft_id)
        self.database.mark_publish_unverified(self.draft_id)
        self.post("publish", caption="caption", confirm="yes")
        self.assertEqual(self.database.get_draft(self.draft_id)["status"], "unverified")
        self.post("confirm-unpublished")
        self.assertEqual(self.database.get_draft(self.draft_id)["status"], "unverified")
        self.post("confirm-unpublished", confirm="yes")
        self.assertEqual(self.database.get_draft(self.draft_id)["status"], "error")


if __name__ == "__main__":
    unittest.main()
