import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from flask import Flask, session, abort
from account_routes import register_account_routes
from account_store import AccountStore, AccountRegistry, folder_id
from config import Config, AccountProfile
from database import Database
from instagram_client import InstagramClient
from meta_oauth import MetaOAuth, OAuthError, SCOPES

TOKEN = "SECRET_TOKEN_NEVER_SEND_TO_BROWSER"
CANDIDATE = dict(ig_id="1234567", username="new_account", page_id="999", page_name="テスト店舗", token=TOKEN,
                 expires_at=(datetime.now(timezone.utc) + timedelta(days=60)).isoformat())
BASE = AccountProfile("existing", "既存", "existing_name", "inbox_old", "posted_old", "11111", "legacy-token", "既存指示")


class AccountOAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.db.init()
        p = patch.multiple(Config, SECRET_KEY="random-test-secret-1234567890123456789012345", ACCOUNT_TOKEN_ENCRYPTION_KEY="",
                           META_APP_ID="app123", META_APP_SECRET="secret123", META_LOGIN_CONFIG_ID="", PUBLIC_BASE_URL="https://example.com")
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(Config, "account_profiles", return_value={BASE.key: BASE})
        p.start()
        self.addCleanup(p.stop)
        self.store = AccountStore(Config, self.db)
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        self.app.config.update(TESTING=True, SECRET_KEY=Config.SECRET_KEY)
        self.app.jinja_env.globals["csrf_token"] = lambda: "csrf"
        self.app.jinja_env.filters["schedule_local"] = str
        self.app.add_url_rule("/", "index", lambda: "dashboard")
        self.app.add_url_rule("/logout", "logout", lambda: "", methods=["POST"])
        def csrf():
            if request.form.get("csrf_token") != "csrf":
                abort(400)
        def login_required(view):
            from functools import wraps
            @wraps(view)
            def wrapped(*a, **kw):
                if not session.get("logged_in"):
                    abort(401)
                return view(*a, **kw)
            return wrapped
        from flask import request
        register_account_routes(self.app, Config, self.store, csrf, login_required)
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s["logged_in"] = True

    def start(self, target=""):
        r = self.client.post("/accounts/connect", data=dict(csrf_token="csrf", account_key=target))
        self.assertEqual(r.status_code, 302)
        return parse_qs(urlsplit(r.location).query)["state"][0]

    def ready(self, candidate=None, target=""):
        state = self.start(target)
        with patch("account_routes.MetaOAuth.discover", return_value=[dict(candidate or CANDIDATE)]):
            r = self.client.get("/accounts/oauth/callback", query_string=dict(state=state, code="code"))
        self.assertEqual(r.location, "/accounts/select")

    def save(self, candidate=None, **settings):
        data = dict(csrf_token="csrf", ig_id=(candidate or CANDIDATE)["ig_id"], label="新店舗", inbox="inbox_new", posted="posted_new")
        data.update(settings)
        return self.client.post("/accounts/save", data=data)

    def test_start_csrf_and_anonymous_access(self):
        self.assertEqual(self.client.post("/accounts/connect").status_code, 400)
        anonymous = self.app.test_client()
        for path in ("/accounts", "/accounts/select", "/accounts/oauth/callback"):
            self.assertEqual(anonymous.get(path).status_code, 401)

    def test_invalid_and_other_session_state_never_contacts_meta(self):
        state = self.start()
        other = self.app.test_client()
        with other.session_transaction() as s:
            s["logged_in"] = True
        with patch("account_routes.MetaOAuth.discover") as discover:
            self.client.get("/accounts/oauth/callback?state=wrong&code=x")
            self.client.get("/accounts/oauth/callback", query_string=dict(state="不正", code="x"))
            other.get("/accounts/oauth/callback", query_string=dict(state=state, code="x"))
        discover.assert_not_called()

    def test_state_expiry_and_replay(self):
        state = self.start()
        with self.db.connect() as c:
            c.execute("UPDATE instagram_oauth_flows SET expires_at=0")
        with patch("account_routes.MetaOAuth.discover") as discover:
            self.client.get("/accounts/oauth/callback", query_string=dict(state=state, code="x"))
        discover.assert_not_called()
        state = self.start()
        with patch("account_routes.MetaOAuth.discover", return_value=[CANDIDATE]) as discover:
            for _ in range(2):
                self.client.get("/accounts/oauth/callback", query_string=dict(state=state, code="x"))
        self.assertEqual(discover.call_count, 1)

    def test_denial_does_not_save_or_render_raw_error(self):
        state = self.start()
        r = self.client.get("/accounts/oauth/callback", query_string=dict(state=state, error="access_denied", error_description=TOKEN), follow_redirects=True)
        self.assertNotIn(TOKEN, r.get_data(as_text=True))
        self.assertEqual(len(self.store.profiles()), 1)

    def test_save_encrypts_tokens_and_persists_across_workers(self):
        self.ready()
        r = self.client.get("/accounts/select")
        self.assertNotIn(TOKEN, r.get_data(as_text=True))
        with self.client.session_transaction() as s:
            self.assertNotIn(TOKEN, json.dumps(dict(s)))
        self.assertNotIn(TOKEN.encode(), Path(self.db.path).read_bytes())
        r = self.save()
        self.assertEqual(r.location, "/accounts/ig_1234567/settings")
        profile = AccountRegistry(AccountStore(Config, self.db))["ig_1234567"]
        self.assertEqual(profile.instagram_access_token, TOKEN)
        self.assertEqual(profile.drive_inbox_folder_id, "inbox_new")
        self.assertNotIn(TOKEN.encode(), Path(self.db.path).read_bytes())
        for path in (r.location, "/accounts"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(TOKEN, response.get_data(as_text=True))

    def test_forged_candidate_and_save_csrf_rejected(self):
        self.ready()
        self.save(dict(CANDIDATE, ig_id="forged"))
        self.assertEqual(len(self.store.profiles()), 1)
        self.assertEqual(self.client.post("/accounts/save").status_code, 400)

    def test_selection_expiry_and_double_save(self):
        self.ready()
        with self.db.connect() as c:
            c.execute("UPDATE instagram_oauth_flows SET expires_at=0")
        self.save()
        self.assertEqual(len(self.store.profiles()), 1)
        self.ready()
        self.save()
        self.save()
        self.assertEqual(len(self.store.profiles()), 2)

    def test_reconnect_keeps_history_schedule_key_and_settings(self):
        self.db.upsert_photo(dict(id="file", name="a.jpg", mimeType="image/jpeg"), "/tmp/a.jpg", "photo", BASE.key)
        draft = self.db.create_draft([self.db.list_photos()[0]["id"]], "caption", BASE.key)
        self.db.schedule_draft(draft, "caption", "2099-01-01T00:00:00+00:00")
        c = dict(CANDIDATE, ig_id=BASE.instagram_ig_user_id, username="renamed")
        self.ready(c, BASE.key)
        self.save(c)
        p = self.store.profiles()[BASE.key]
        self.assertEqual((p.username, p.drive_inbox_folder_id, p.caption_prompt), ("renamed", BASE.drive_inbox_folder_id, BASE.caption_prompt))
        self.assertEqual(self.db.get_draft(draft)["status"], "scheduled")
        self.assertEqual(self.db.get_draft(draft)["account_key"], BASE.key)
        self.assertEqual(len(self.store.profiles()), 1)

    def test_reconnect_refuses_different_ig_account(self):
        state = self.start(BASE.key)
        with patch("account_routes.MetaOAuth.discover", return_value=[CANDIDATE]):
            r = self.client.get("/accounts/oauth/callback", query_string=dict(state=state, code="x"))
        self.assertEqual(r.location, "/accounts")
        self.assertEqual(self.store.profiles()[BASE.key].instagram_access_token, "legacy-token")

    def test_legacy_empty_id_adopted_by_verified_username(self):
        p = AccountProfile(BASE.key, BASE.label, CANDIDATE["username"], BASE.drive_inbox_folder_id, BASE.drive_posted_folder_id, "", "", BASE.caption_prompt)
        with patch.object(Config, "account_profiles", return_value={BASE.key: p}):
            self.assertEqual(self.store.profiles(), {})
            empty_page = self.client.get("/accounts")
            self.assertIn("登録済みアカウントはありません", empty_page.get_data(as_text=True))
            self.ready()
            self.save()
            self.assertEqual(len(self.store.profiles()), 1)
            self.assertEqual(self.store.profiles()[BASE.key].instagram_ig_user_id, CANDIDATE["ig_id"])

    def test_same_account_addition_updates_instead_of_duplicating(self):
        for _ in range(2):
            self.ready()
            self.save()
        self.assertEqual(len(self.store.profiles()), 2)

    def test_shared_folder_rejected_and_settings_editable(self):
        self.ready()
        self.save(inbox=BASE.drive_inbox_folder_id)
        self.assertEqual(len(self.store.profiles()), 1)
        self.save()
        r = self.client.post("/accounts/ig_1234567/settings", data=dict(csrf_token="csrf", label="改名", inbox="https://drive.google.com/drive/u/0/folders/folder_123?usp=sharing", posted="folder_456", caption_prompt="新指示"))
        self.assertEqual(r.status_code, 302)
        p = self.store.profiles()["ig_1234567"]
        self.assertEqual((p.label, p.drive_inbox_folder_id, p.caption_prompt), ("改名", "folder_123", "新指示"))

    def test_existing_env_account_drive_settings_are_editable_before_meta_auth(self):
        page = self.client.get("/accounts/existing/settings")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('value="inbox_old"', html)
        self.assertIn('value="posted_old"', html)

        response = self.client.post(
            "/accounts/existing/settings",
            data=dict(
                csrf_token="csrf", label="既存アカウント更新",
                inbox="https://drive.google.com/drive/u/0/folders/inbox_updated_123",
                posted="posted_updated_456", caption_prompt="画面で更新した指示",
            ),
        )
        self.assertEqual(response.status_code, 302)
        profile = AccountRegistry(AccountStore(Config, self.db))["existing"]
        self.assertFalse(profile.oauth_connected)
        self.assertEqual(profile.label, "既存アカウント更新")
        self.assertEqual(profile.drive_inbox_folder_id, "inbox_updated_123")
        self.assertEqual(profile.drive_posted_folder_id, "posted_updated_456")
        self.assertEqual(profile.caption_prompt, "画面で更新した指示")
        self.assertEqual(profile.instagram_access_token, "legacy-token")

        # 環境変数側のDrive IDが変化しても、管理画面で保存した値を優先する。
        changed = AccountProfile(
            BASE.key, BASE.label, BASE.username, "env_changed_inbox",
            "env_changed_posted", BASE.instagram_ig_user_id,
            BASE.instagram_access_token, BASE.caption_prompt,
        )
        with patch.object(Config, "account_profiles", return_value={BASE.key: changed}):
            reloaded = AccountStore(Config, self.db).profiles()[BASE.key]
        self.assertEqual(reloaded.drive_inbox_folder_id, "inbox_updated_123")
        self.assertEqual(reloaded.drive_posted_folder_id, "posted_updated_456")

    def test_existing_env_account_settings_still_prevent_shared_drive_folders(self):
        self.ready()
        self.save()
        response = self.client.post(
            "/accounts/existing/settings",
            data=dict(
                csrf_token="csrf", label="既存", inbox="inbox_new",
                posted="posted_existing_new", caption_prompt="指示",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.profiles()["existing"].drive_inbox_folder_id, "inbox_old")

    def test_cancel_removes_pending_credentials(self):
        self.ready()
        self.client.post("/accounts/cancel", data=dict(csrf_token="csrf"))
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM instagram_oauth_flows").fetchone()[0], 0)

    def test_lost_key_and_expired_token_block_publishing(self):
        self.ready()
        self.save()
        with patch.object(Config, "SECRET_KEY", "other-secret-12345678901234567890123456789"):
            p = self.store.profiles()["ig_1234567"]
            self.assertTrue(p.connection_error)
            with self.assertRaisesRegex(RuntimeError, "暗号鍵"):
                InstagramClient(Config, p)
        with self.db.connect() as c:
            c.execute("UPDATE instagram_accounts SET token_expires_at='2020-01-01T00:00:00+00:00'")
        with self.assertRaisesRegex(RuntimeError, "認証期限"):
            InstagramClient(Config, self.store.profiles()["ig_1234567"])
        self.assertIn("期限切れ", self.client.get("/accounts").get_data(as_text=True))

    def test_cron_uses_new_account(self):
        self.ready()
        self.save()
        self.db.upsert_photo(dict(id="newfile", name="a.jpg", mimeType="image/jpeg"), "/tmp/a.jpg", "photo", "ig_1234567")
        draft = self.db.create_draft([self.db.list_photos()[0]["id"]], "caption", "ig_1234567")
        self.db.schedule_draft(draft, "caption", "2020-01-01T00:00:00+00:00")
        import run_scheduled
        def publish(database, config, profiles, draft_id):
            self.assertEqual(profiles["ig_1234567"].instagram_access_token, TOKEN)
            self.assertEqual(draft_id, draft)
            return {"media_id": "mock"}
        with patch.object(Config, "DATABASE_PATH", Path(self.db.path)), patch.object(Config, "LOG_FILE", Path(self.tmp.name) / "cron.log"), patch("run_scheduled.publish_claimed_draft", side_effect=publish) as mocked:
            run_scheduled.main()
        mocked.assert_called_once()

    def test_weak_secret_disables_auth(self):
        with patch.object(Config, "SECRET_KEY", "dev-only-change-me"):
            r = self.client.post("/accounts/connect", data=dict(csrf_token="csrf"))
        self.assertEqual(r.location, "/accounts")


class MetaClientTest(unittest.TestCase):
    def setUp(self):
        self.oauth = MetaOAuth(Config)
        p = patch.multiple(Config, META_APP_ID="app123", META_APP_SECRET="secret123", META_LOGIN_CONFIG_ID="", PUBLIC_BASE_URL="https://example.com/subapp")
        p.start()
        self.addCleanup(p.stop)

    def responses(self, data):
        self.oauth.http.get = Mock(side_effect=[Mock(ok=True, json=Mock(return_value=d)) for d in data])

    def data(self):
        return [dict(access_token="short"), dict(access_token=TOKEN, expires_in=5184000),
                dict(data=dict(is_valid=True, app_id="app123", expires_at=int(time.time()) + 5184000)),
                dict(data=[dict(permission=s, status="granted") for s in SCOPES]),
                dict(data=[dict(id="999", name="店舗", instagram_business_account=dict(id="1234567"))]),
                dict(id="1234567", username="new_account")]

    def test_business_and_classic_login_urls(self):
        query = parse_qs(urlsplit(self.oauth.authorization_url("state")).query)
        self.assertEqual(query["redirect_uri"], ["https://example.com/subapp/accounts/oauth/callback"])
        self.assertEqual(set(query["scope"][0].split(",")), set(SCOPES))
        self.assertNotIn("secret123", self.oauth.authorization_url("state"))
        with patch.object(Config, "META_LOGIN_CONFIG_ID", "config123"):
            query = parse_qs(urlsplit(self.oauth.authorization_url("state")).query)
        self.assertEqual(query["config_id"], ["config123"])
        self.assertNotIn("scope", query)

    def test_long_token_exchange_validation_and_discovery(self):
        self.responses(self.data())
        result = self.oauth.discover("code")
        self.assertEqual(result[0]["token"], TOKEN)
        self.assertEqual(result[0]["ig_id"], "1234567")
        calls = self.oauth.http.get.call_args_list
        self.assertEqual(calls[0].kwargs["params"]["redirect_uri"], self.oauth.redirect_uri)
        self.assertEqual(calls[1].kwargs["params"]["grant_type"], "fb_exchange_token")
        self.assertEqual(calls[4].kwargs["headers"]["Authorization"], "Bearer " + TOKEN)

    def test_missing_permission_and_wrong_app_rejected(self):
        data = self.data()
        data[3]["data"] = [dict(permission="instagram_basic", status="granted")]
        self.responses(data)
        with self.assertRaisesRegex(OAuthError, "権限が不足"):
            self.oauth.discover("code")
        data = self.data()
        data[2]["data"]["app_id"] = "other"
        self.responses(data)
        with self.assertRaisesRegex(OAuthError, "確認できません"):
            self.oauth.discover("code")

    def test_missing_link_and_pagination(self):
        data = self.data()
        data[4] = dict(data=[dict(id="999", name="No IG")])
        self.responses(data)
        with self.assertRaisesRegex(OAuthError, "Facebookページとの連携"):
            self.oauth.discover("code")
        data = self.data()
        data.insert(4, dict(data=[], paging=dict(next="https://evil.example", cursors=dict(after="cursor2"))))
        self.responses(data)
        self.assertEqual(len(self.oauth.discover("code")), 1)
        call = self.oauth.http.get.call_args_list[5]
        self.assertIn("graph.facebook.com", call.args[0])
        self.assertEqual(call.kwargs["params"]["after"], "cursor2")

    def test_api_and_network_errors_never_expose_secrets(self):
        self.oauth.http.get = Mock(return_value=Mock(ok=False, json=Mock(return_value=dict(error=dict(code=190, message=TOKEN)))))
        with self.assertRaises(OAuthError) as cm:
            self.oauth.discover("code")
        self.assertNotIn(TOKEN, str(cm.exception))
        import requests
        self.oauth.http.get = Mock(side_effect=requests.RequestException(TOKEN))
        with self.assertRaises(OAuthError) as cm:
            self.oauth.discover("code")
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_drive_url_validation(self):
        self.assertEqual(folder_id("https://drive.google.com/drive/u/0/folders/folder_123?usp=sharing"), "folder_123")
        with self.assertRaises(ValueError):
            folder_id("https://evil.example/folders/folder_123")


if __name__ == "__main__":
    unittest.main()
