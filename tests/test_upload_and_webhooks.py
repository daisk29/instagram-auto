import base64
import hashlib
import hmac
import io
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from flask import Flask
from PIL import Image

import app as app_module
from database import Database
from direct_upload import register_direct_upload_routes
from line_drive_common import db as line_db


class RegisteredRoutesTest(TestCase):
    def test_signed_webhooks_use_separate_channels(self):
        payload = b'{"events":[]}'
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "LINE_DRIVE_DATA_DIR": tmp,
            "LINE_CHANNEL_SECRET": "fixture-bisyu-secret",
            "LINE_CHANNEL_ACCESS_TOKEN": "fixture-bisyu-token",
            "SHARED_LINE_CHANNEL_SECRET": "fixture-shared-secret",
            "SHARED_LINE_CHANNEL_ACCESS_TOKEN": "fixture-shared-token",
        }), closing(line_db()) as connection, patch(
            "line_drive_bridge.db", return_value=connection
        ), closing(sqlite3.connect(":memory:")) as shared, patch(
            "shared_line_bridge.database", return_value=shared
        ):
            client = app_module.app.test_client()
            for path, valid, wrong in (
                ("/line/webhook", "fixture-bisyu-secret", "fixture-shared-secret"),
                ("/line/shared/webhook", "fixture-shared-secret", "fixture-bisyu-secret"),
            ):
                def post(key):
                    signature = base64.b64encode(hmac.new(key.encode(), payload, hashlib.sha256).digest())
                    return client.post(path, data=payload, content_type="application/json",
                                       headers={"X-Line-Signature": signature.decode()})
                self.assertEqual(client.get(path).status_code, 405)
                self.assertEqual(post(wrong).status_code, 403)
                self.assertEqual(post(valid).get_json(), {"ok": True})

    def test_dashboard_upload_requires_login_and_csrf(self):
        client = app_module.app.test_client()
        self.assertEqual(client.post("/direct-upload/photo").status_code, 302)
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["csrf_token"] = "expected"
        self.assertEqual(client.post("/direct-upload/photo").status_code, 400)

    def test_dashboard_upload_imports_photo_for_selected_account(self):
        with TemporaryDirectory() as tmp, patch("direct_upload.DriveClient") as drive:
            app = Flask(__name__)
            database = Database(Path(tmp) / "app.sqlite3")
            database.init()
            account = SimpleNamespace(key="crach29", drive_inbox_folder_id="fixture_crach_inbox")
            profile = Mock(return_value=account)
            verify_csrf = Mock()
            register_direct_upload_routes(app, SimpleNamespace(MEDIA_DIR=Path(tmp) / "media"),
                                          database, profile, lambda fn: fn, verify_csrf)
            create = drive.return_value.service.return_value.files.return_value.create
            create.return_value.execute.return_value = {"id": "fixture_image_123"}
            photo = io.BytesIO()
            Image.new("RGB", (100, 100), "red").save(photo, format="PNG")
            photo.seek(0)
            response = app.test_client().post(
                "/direct-upload/photo", data={"account_key": "crach29", "photo": (photo, "photo.png")}
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            profile.assert_called_once_with("crach29")
            verify_csrf.assert_called_once()
            self.assertEqual(create.call_args.kwargs["body"]["parents"], ["fixture_crach_inbox"])
            saved, = database.list_photos("inbox", "crach29")
            self.assertEqual(saved["drive_file_id"], "fixture_image_123")
            self.assertTrue(Path(saved["local_path"]).is_file())
