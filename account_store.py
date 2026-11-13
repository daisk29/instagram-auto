"""環境変数の既存アカウントとDBで追加したアカウントを統合する。"""
import base64
import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import replace
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from config import AccountProfile
from database import utcnow


class AccountStore:
    def __init__(self, config, database):
        self.config, self.db = config, database
        self.init()

    def init(self):
        with self.db.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS instagram_accounts (
                    account_key TEXT PRIMARY KEY,
                    ig_user_id TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    label TEXT NOT NULL,
                    token_encrypted TEXT NOT NULL,
                    token_expires_at TEXT NOT NULL DEFAULT '',
                    page_name TEXT NOT NULL DEFAULT '',
                    drive_inbox_folder_id TEXT NOT NULL DEFAULT '',
                    drive_posted_folder_id TEXT NOT NULL DEFAULT '',
                    caption_prompt TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instagram_oauth_flows (
                    flow_hash TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    payload_encrypted TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instagram_account_settings (
                    account_key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    drive_inbox_folder_id TEXT NOT NULL DEFAULT '',
                    drive_posted_folder_id TEXT NOT NULL DEFAULT '',
                    caption_prompt TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
            """)

    def cipher(self):
        explicit = self.config.ACCOUNT_TOKEN_ENCRYPTION_KEY
        if explicit:
            try:
                return Fernet(explicit.encode())
            except (ValueError, TypeError):
                raise ValueError("ACCOUNT_TOKEN_ENCRYPTION_KEYの形式が正しくありません。") from None
        secret = self.config.SECRET_KEY
        if len(secret) < 32 or secret in ("dev-only-change-me", "change-to-a-long-random-string"):
            raise ValueError("APP_SECRET_KEYを32文字以上のランダムな値に設定してください。")
        key = hashlib.sha256(("instagram-account-tokens:v1:" + secret).encode()).digest()
        return Fernet(base64.urlsafe_b64encode(key))

    def configuration_error(self):
        try:
            self.cipher()
        except ValueError as exc:
            return str(exc)
        return ""

    def encrypt(self, data):
        return self.cipher().encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()

    def decrypt(self, data):
        try:
            return json.loads(self.cipher().decrypt(data.encode()))
        except (InvalidToken, ValueError, TypeError):
            raise ValueError("保存した認証情報を読み込めません。暗号鍵を確認するか再認証してください。") from None

    @staticmethod
    def flow_hash(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def new_flow(self, value, owner, stage, payload):
        encrypted = self.encrypt(payload)
        with self.db.connect() as conn:
            conn.execute("DELETE FROM instagram_oauth_flows WHERE expires_at <= ?", (int(time.time()),))
            conn.execute("INSERT INTO instagram_oauth_flows VALUES (?, ?, ?, ?, ?)",
                         (self.flow_hash(value), owner, stage, encrypted, int(time.time()) + 600))

    def read_flow(self, value, owner, stage, consume=False):
        if not value or not owner:
            return None
        with self.db.connect() as conn:
            if consume:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM instagram_oauth_flows WHERE flow_hash=? AND owner=? AND stage=? AND expires_at>?",
                (self.flow_hash(value), owner, stage, int(time.time()))).fetchone()
            if not row:
                return None
            if consume:
                conn.execute("DELETE FROM instagram_oauth_flows WHERE flow_hash=?", (row["flow_hash"],))
            payload = self.decrypt(row["payload_encrypted"])
        return payload

    def clear_flows(self, owner):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM instagram_oauth_flows WHERE owner=? OR expires_at<=?", (owner, int(time.time())))

    def profiles(self):
        # .envの固定枠は、Instagram IDまたはトークンが残っている場合だけ
        # 移行用アカウントとして表示する。両方を空にした初期状態では0件にする。
        result = {
            key: profile
            for key, profile in self.config.account_profiles().items()
            if profile.instagram_ig_user_id or profile.instagram_access_token
        }
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM instagram_accounts ORDER BY created_at, account_key").fetchall()
            setting_rows = conn.execute(
                "SELECT * FROM instagram_account_settings ORDER BY account_key"
            ).fetchall()
        for row in rows:
            error = ""
            try:
                token = self.decrypt(row["token_encrypted"])
            except ValueError as exc:
                token, error = "", str(exc)
            result[row["account_key"]] = AccountProfile(
                key=row["account_key"], label=row["label"], username=row["username"],
                drive_inbox_folder_id=row["drive_inbox_folder_id"],
                drive_posted_folder_id=row["drive_posted_folder_id"],
                instagram_ig_user_id=row["ig_user_id"], instagram_access_token=token,
                caption_prompt=row["caption_prompt"], token_expires_at=row["token_expires_at"],
                oauth_connected=True, page_name=row["page_name"], connection_error=error)
        # 管理画面で保存した設定を、.env由来・Meta認証由来の双方へ適用する。
        # .env自体を書き換えないため、既存アカウントも安全に画面管理へ移行できる。
        for row in setting_rows:
            profile = result.get(row["account_key"])
            if profile:
                result[row["account_key"]] = replace(
                    profile,
                    label=row["label"],
                    drive_inbox_folder_id=row["drive_inbox_folder_id"],
                    drive_posted_folder_id=row["drive_posted_folder_id"],
                    caption_prompt=row["caption_prompt"],
                )
        return result

    def save_connection(self, flow, owner, ig_id, settings):
        """認証候補の消費とアカウント更新を1トランザクションにまとめる。"""
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM instagram_oauth_flows WHERE flow_hash=? AND owner=? AND stage='ready' AND expires_at>?",
                               (self.flow_hash(flow), owner, int(time.time()))).fetchone()
            if not row:
                raise ValueError("認証の有効時間が切れました。最初から認証してください。")
            payload = self.decrypt(row["payload_encrypted"])
            candidate = next((c for c in payload["candidates"] if c["ig_id"] == ig_id), None)
            if not candidate:
                raise ValueError("認証した一覧からアカウントを選択してください。")
            profiles = self.profiles()
            target = payload.get("target")
            existing = profiles.get(target) if target else None
            if target and not existing:
                raise ValueError("再認証対象のアカウントが見つかりません。")
            if existing:
                matches = (existing.instagram_ig_user_id == ig_id if existing.instagram_ig_user_id
                           else existing.username.casefold() == candidate["username"].casefold())
                if not matches:
                    raise ValueError("再認証では元と同じInstagramアカウントを選択してください。")
            else:
                existing = next((p for p in profiles.values() if p.instagram_ig_user_id == ig_id), None)
                if not existing:
                    existing = next((p for p in profiles.values() if not p.instagram_ig_user_id
                                     and p.username.casefold() == candidate["username"].casefold()), None)
            if existing:
                key = existing.key
            else:
                # 固定枠を初期化した後でも、同じユーザー名なら従来のkeyを再利用する。
                # これにより既存の写真・投稿履歴・予約を新しい認証へ結び直せる。
                legacy = next(
                    (
                        p for p in self.config.account_profiles().values()
                        if p.username.casefold() == candidate["username"].casefold()
                    ),
                    None,
                )
                key = legacy.key if legacy else "ig_" + ig_id
            duplicate = next((p for p in profiles.values() if p.key != key and p.instagram_ig_user_id == ig_id), None)
            if duplicate:
                raise ValueError("このInstagramアカウントは別の設定で登録済みです。そちらから再認証してください。")
            # 再認証はDrive・AI設定を維持する。新規の場合のみ設定フォームを適用。
            if existing:
                settings = dict(label=existing.label, inbox=existing.drive_inbox_folder_id,
                                posted=existing.drive_posted_folder_id, prompt=existing.caption_prompt)
            self.validate_folders(settings, profiles, key)
            now = utcnow()
            conn.execute("""
                INSERT INTO instagram_accounts
                    (account_key, ig_user_id, username, label, token_encrypted, token_expires_at,
                     page_name, drive_inbox_folder_id, drive_posted_folder_id, caption_prompt, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    ig_user_id=excluded.ig_user_id, username=excluded.username, label=excluded.label,
                    token_encrypted=excluded.token_encrypted, token_expires_at=excluded.token_expires_at,
                    page_name=excluded.page_name, drive_inbox_folder_id=excluded.drive_inbox_folder_id,
                    drive_posted_folder_id=excluded.drive_posted_folder_id, caption_prompt=excluded.caption_prompt,
                    updated_at=excluded.updated_at
            """, (key, ig_id, candidate["username"], settings["label"], self.encrypt(candidate["token"]),
                  candidate["expires_at"], candidate["page_name"], settings["inbox"], settings["posted"],
                  settings["prompt"], now, now))
            conn.execute("DELETE FROM instagram_oauth_flows WHERE flow_hash=?", (row["flow_hash"],))
        return key

    def update_settings(self, key, settings):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            profiles = self.profiles()
            profile = profiles.get(key)
            if not profile:
                raise ValueError("アカウントが見つかりません。")
            self.validate_folders(settings, profiles, key)
            conn.execute("""
                INSERT INTO instagram_account_settings
                    (account_key, label, drive_inbox_folder_id,
                     drive_posted_folder_id, caption_prompt, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    label=excluded.label,
                    drive_inbox_folder_id=excluded.drive_inbox_folder_id,
                    drive_posted_folder_id=excluded.drive_posted_folder_id,
                    caption_prompt=excluded.caption_prompt,
                    updated_at=excluded.updated_at
            """, (key, settings["label"], settings["inbox"], settings["posted"],
                  settings["prompt"], utcnow()))

    @staticmethod
    def validate_folders(settings, profiles, key):
        selected = {value for value in (settings["inbox"], settings["posted"]) if value}
        for profile in profiles.values():
            if profile.key != key and selected.intersection(
                    (profile.drive_inbox_folder_id, profile.drive_posted_folder_id)):
                raise ValueError("このDriveフォルダは別のアカウントで使用中です。専用フォルダを指定してください。")


class AccountRegistry(Mapping):
    """Web/cron/複数workerが毎回DBの最新設定を参照する。"""
    def __init__(self, store):
        self.store = store

    def __getitem__(self, key):
        return self.store.profiles()[key]

    def __iter__(self):
        return iter(self.store.profiles())

    def __len__(self):
        return len(self.store.profiles())

    def values(self):
        return self.store.profiles().values()


def folder_id(value):
    value = value.strip()
    if not value:
        return ""
    if value.startswith("https://"):
        parsed = urlsplit(value)
        match = re.search(r"/folders/([A-Za-z0-9_-]+)(?:/|$)", parsed.path)
        if parsed.hostname != "drive.google.com" or not match:
            raise ValueError("Google DriveのフォルダURLまたはフォルダIDを入力してください。")
        value = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,200}", value):
        raise ValueError("DriveフォルダIDの形式が正しくありません。")
    return value


def parse_settings(form, default_prompt):
    label = form.get("label", "").strip()
    prompt = form.get("caption_prompt", "").strip() or default_prompt
    if not label or len(label) > 100 or len(prompt) > 10000:
        raise ValueError("表示名は1〜100文字、投稿文の指示は10,000文字以内で入力してください。")
    inbox, posted = folder_id(form.get("inbox", "")), folder_id(form.get("posted", ""))
    if inbox and posted and inbox == posted:
        raise ValueError("inboxとpostedには別々のDriveフォルダを指定してください。")
    # 写真の所有先が変わらないよう、既存アカウントと同じフォルダはroutes側でも検査。
    return dict(label=label, inbox=inbox, posted=posted, prompt=prompt)
