import base64
from contextlib import ExitStack, closing
import hashlib
import hmac
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask

import bisyu_line_flow as flow
from database import Database
from line_drive_bridge import line_drive_blueprint, process_event
from line_drive_common import db

OWNER = "U" + "a" * 32
OTHER = "U" + "b" * 32
FOLDER = "fixture_bisyu_inbox"


class BisyuFlowTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        temp = stack.enter_context(TemporaryDirectory())
        stack.enter_context(patch("caption_client.OpenAI"))
        stack.enter_context(patch.dict(os.environ, {
            "LINE_DRIVE_DATA_DIR": temp,
            "LINE_ALLOWED_USER_IDS": f"{OWNER},{OTHER}",
            "LINE_CHANNEL_ACCESS_TOKEN": "fixture-bisyu-token",
            "LINE_CHANNEL_SECRET": "fixture-bisyu-secret",
            "SHARED_LINE_CHANNEL_ACCESS_TOKEN": "fixture-shared-token",
            "SHARED_LINE_ENABLE_POSTING": "0",
            "BISYU_LINE_ENABLE_CAPTION_REPLY": "1",
            "BISYU_LINE_ENABLE_POSTING": "1",
            "BISYU_LINE_APPROVER_IDS": OWNER,
            "BISYU_DRIVE_INBOX_FOLDER_ID": FOLDER,
            "LINE_CAPTION_WINDOW_SECONDS": "180",
        }))
        self.conn = stack.enter_context(closing(db()))
        self.database = Database(Path(temp) / "app.sqlite3")
        self.database.init()
        self.file = Path(temp) / "photo.jpg"
        self.file.write_bytes(b"fixture")
        self.accounts = {"bisyu": SimpleNamespace(
            key="bisyu", username="bisyu_1995", drive_inbox_folder_id=FOLDER,
            drive_posted_folder_id="fixture_posted",
            instagram_ig_user_id="fixture-ig-user", instagram_access_token="fixture-ig-token",
        )}
        self.config = SimpleNamespace(PUBLIC_BASE_URL="https://instagram.example.com")
        self.now = int(time.time() * 1000)

    def message(self, kind, message_id, offset=0, user=OWNER):
        return {
            "type": "message", "source": {"type": "user", "userId": user},
            "timestamp": self.now + offset, "message": {
                "type": kind, "id": message_id, "text": "新メニューのお知らせ",
            },
        }

    def receive(self, event):
        return process_event(self.conn, event)

    def queue(self):
        self.assertTrue(self.receive(self.message("image", "photo1")))
        self.assertTrue(self.receive(self.message("image", "photo2", 1)))
        self.assertTrue(self.receive(self.message("text", "note11", 2)))
        self.assertFalse(self.receive(self.message("text", "note11", 2)))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bisyu_line_proposals").fetchone()[0], 1)
        return self.proposal()

    def complete_uploads(self):
        for index in (1, 2):
            file_id = f"fixture_file_{index}"
            self.database.upsert_photo(
                {"id": file_id, "name": f"photo{index}.jpg", "mimeType": "image/jpeg"},
                self.file, f"fixture-photo-token-{index}", "bisyu",
            )
            self.conn.execute(
                "UPDATE jobs SET status='done',google_file_id=? WHERE message_id=?",
                (file_id, f"photo{index}"),
            )
        self.conn.commit()

    def proposal(self):
        return self.conn.execute("SELECT * FROM bisyu_line_proposals").fetchone()

    def preview(self):
        self.queue()
        self.complete_uploads()
        with patch("caption_client.CaptionClient.generate", return_value="美趣のキャプション案") as generate:
            self.accounts["bisyu"].caption_prompt = "美趣の指示"
            self.config.OPENAI_API_KEY = "fixture"
            self.config.OPENAI_MODEL = "fixture"
            flow.create_proposal(self.conn, self.proposal(), self.config, self.database, self.accounts)
            generate.assert_called_once()
        outbox = self.conn.execute("SELECT * FROM bisyu_line_outbox").fetchone()
        messages = json.loads(outbox["messages"])
        self.assertIn("美趣のキャプション案", messages[0]["text"])
        return messages

    def approval(self, messages, user=OWNER):
        data = messages[1]["template"]["actions"][0]["data"]
        return {"type": "postback", "source": {"type": "user", "userId": user}, "postback": {"data": data}}

    def test_end_to_end_reply_approve_and_publish_only_once(self):
        messages = self.preview()
        proposal = self.proposal()
        self.assertEqual("ready", proposal["state"])
        self.assertEqual("draft", self.database.get_draft(proposal["draft_id"])["status"])
        with patch("publish_service.publish_claimed_draft") as publish:
            def publish_stub(database, config, accounts, draft_id):
                self.assertEqual({"bisyu"}, set(accounts))
                self.assertEqual("publishing", database.get_draft(draft_id)["status"])
                database.begin_instagram_publish(draft_id)
                database.finish_publish(draft_id, "fixture-media-id", "https://www.instagram.com/p/fixture/")
            publish.side_effect = publish_stub
            self.assertFalse(self.receive(self.approval(messages, OTHER)))
            with patch.dict(os.environ, {"BISYU_LINE_APPROVER_IDS": OTHER}):
                self.assertFalse(self.receive(self.approval(messages, OTHER)))
            publish.assert_not_called()
            self.assertTrue(self.receive(self.approval(messages)))
            self.assertFalse(self.receive(self.approval(messages)))
            with patch("bisyu_line_flow.send_outbox"):
                flow.process(self.conn, self.config, self.database, self.accounts)
                flow.process(self.conn, self.config, self.database, self.accounts)
            publish.assert_called_once()
        self.assertEqual("posted", self.proposal()["state"])
        self.assertIn("https://www.instagram.com/p/fixture/", self.conn.execute(
            "SELECT messages FROM bisyu_line_outbox ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0])

    def test_caption_is_queued_only_once_and_waits_for_all_uploads(self):
        proposal = self.queue()
        with patch("caption_client.CaptionClient") as client:
            flow.create_proposal(self.conn, proposal, self.config, self.database, self.accounts)
            client.assert_not_called()
        self.assertEqual("queued", self.proposal()["state"])
        self.assertEqual([], self.database.list_drafts("bisyu"))

    def test_import_checks_actual_drive_parent_and_stores_bisyu_only(self):
        self.config.MEDIA_DIR = self.file.parent / "media"
        account = self.accounts["bisyu"]
        job = {"google_file_id": "fixture_drive_photo"}
        with patch("drive_client.DriveClient") as drive, patch(
            "googleapiclient.http.MediaIoBaseDownload"
        ) as download, patch("image_prepare.prepare_instagram_image") as prepare:
            files = drive.return_value.service.return_value.files.return_value
            item = {"id": job["google_file_id"], "name": "photo.jpg", "mimeType": "image/jpeg"}
            files.get.return_value.execute.return_value = dict(item, parents=["another_folder"])
            with self.assertRaises(flow.FlowError):
                flow.import_photos(self.database, self.config, account, [job])
            files.get_media.assert_not_called()
            files.get.return_value.execute.return_value = dict(item, parents=[FOLDER])
            download.return_value.next_chunk.return_value = (Mock(), True)
            prepare.side_effect = lambda source, target: target.write_bytes(b"normalized")
            ids = flow.import_photos(self.database, self.config, account, [job])
            self.assertEqual("bisyu", self.database.get_photos(ids)[0]["account_key"])
            self.assertEqual(b"normalized", (self.config.MEDIA_DIR / "fixture_drive_photo.jpg").read_bytes())

    def test_reply_disabled_keeps_existing_caption_storage(self):
        with patch.dict(os.environ, {"BISYU_LINE_ENABLE_CAPTION_REPLY": "0"}):
            self.assertTrue(self.receive(self.message("image", "photo1")))
            self.assertTrue(self.receive(self.message("text", "note11", 1)))
        self.assertEqual("新メニューのお知らせ", self.conn.execute(
            "SELECT caption_text FROM jobs WHERE message_id='photo1'"
        ).fetchone()[0])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='bisyu_line_proposals'"
        ).fetchone())

    def test_other_sender_cannot_supply_reference(self):
        self.receive(self.message("image", "photo1"))
        self.assertFalse(self.receive(self.message("text", "note11", 1, user=OTHER)))

    def test_preview_without_posting_has_no_approval_button(self):
        with patch.dict(os.environ, {"BISYU_LINE_ENABLE_POSTING": "0", "SHARED_LINE_ENABLE_POSTING": "1"}):
            self.assertTrue(all(m["type"] == "text" for m in self.preview()))

    def test_wrong_webhook_signature_cannot_approve(self):
        messages = self.preview()
        app = Flask(__name__)
        app.register_blueprint(line_drive_blueprint)
        client = app.test_client()
        payload = json.dumps({"events": [self.approval(messages)]}).encode()

        def post(secret):
            signature = base64.b64encode(hmac.new(secret, payload, hashlib.sha256).digest()).decode()
            return client.post("/line/webhook", data=payload, headers={"X-Line-Signature": signature})

        with patch("line_drive_bridge.dispatch_worker") as dispatch:
            self.assertEqual(403, post(b"wrong").status_code)
            dispatch.assert_not_called()
            self.assertEqual("ready", self.proposal()["state"])
            self.assertEqual(200, post(b"fixture-bisyu-secret").status_code)
            dispatch.assert_called_once()
        self.assertEqual("approved", self.proposal()["state"])

    def test_changed_caption_is_not_published(self):
        messages = self.preview()
        self.database.update_editable_draft(self.proposal()["draft_id"], "別の文章")
        self.assertTrue(self.receive(self.approval(messages)))
        with patch("publish_service.publish_claimed_draft") as publish, patch("bisyu_line_flow.send_outbox"):
            flow.process(self.conn, self.config, self.database, self.accounts)
            publish.assert_not_called()
        self.assertEqual("failed", self.proposal()["state"])
        self.assertEqual("別の文章", self.database.get_draft(self.proposal()["draft_id"])["caption"])

    def test_revoked_approval_is_not_published(self):
        messages = self.preview()
        self.assertTrue(self.receive(self.approval(messages)))
        with patch("publish_service.publish_claimed_draft") as publish, patch(
            "bisyu_line_flow.send_outbox"
        ), patch.dict(os.environ, {"BISYU_LINE_ENABLE_POSTING": "0"}):
            flow.process(self.conn, self.config, self.database, self.accounts)
            publish.assert_not_called()
        self.assertEqual("failed", self.proposal()["state"])

    def test_changed_instagram_account_is_not_published(self):
        messages = self.preview()
        self.assertTrue(self.receive(self.approval(messages)))
        self.accounts["bisyu"].instagram_ig_user_id = "different-ig-id"
        with patch("publish_service.publish_claimed_draft") as publish, patch("bisyu_line_flow.send_outbox"):
            flow.process(self.conn, self.config, self.database, self.accounts)
            publish.assert_not_called()
        self.assertEqual("failed", self.proposal()["state"])

    def test_draft_is_recovered_without_second_ai_call_after_interruption(self):
        self.preview()
        proposal = self.proposal()
        self.conn.execute("UPDATE bisyu_line_proposals SET draft_id=NULL,state='queued'")
        self.conn.commit()
        with patch("caption_client.CaptionClient") as client:
            flow.create_proposal(self.conn, self.proposal(), self.config, self.database, self.accounts)
            client.assert_not_called()
        self.assertEqual(proposal["draft_id"], self.proposal()["draft_id"])
        self.assertEqual(1, len(self.database.list_drafts("bisyu")))

    def test_push_uses_bisyu_token_and_duplicate_is_not_resent(self):
        self.preview()
        row = self.conn.execute("SELECT retry_key FROM bisyu_line_outbox").fetchone()
        with patch("requests.post") as post:
            response = post.return_value.__enter__.return_value
            response.status_code = 409
            response.headers = {"X-Line-Accepted-Request-Id": "accepted-fixture"}
            flow.send_outbox(self.conn)
            flow.send_outbox(self.conn)
            post.assert_called_once()
            self.assertEqual("Bearer fixture-bisyu-token", post.call_args.kwargs["headers"]["Authorization"])
            self.assertEqual(row["retry_key"], post.call_args.kwargs["headers"]["X-Line-Retry-Key"])
