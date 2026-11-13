import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _path(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default))
    return value if value.is_absolute() else BASE_DIR / value


@dataclass(frozen=True)
class AccountProfile:
    key: str
    label: str
    username: str
    drive_inbox_folder_id: str
    drive_posted_folder_id: str
    instagram_ig_user_id: str
    instagram_access_token: str
    caption_prompt: str
    token_expires_at: str = ""
    oauth_connected: bool = False
    page_name: str = ""
    connection_error: str = ""


class Config:
    SECRET_KEY = os.getenv("APP_SECRET_KEY") or secrets.token_urlsafe(48)
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

    GOOGLE_CREDENTIALS_FILE = _path(
        "GOOGLE_CREDENTIALS_FILE", "credentials/google_oauth_credentials.json"
    )
    GOOGLE_TOKEN_FILE = _path("GOOGLE_TOKEN_FILE", "credentials/token.json")
    DRIVE_INBOX_FOLDER_ID = os.getenv("DRIVE_INBOX_FOLDER_ID", "")
    DRIVE_POSTED_FOLDER_ID = os.getenv("DRIVE_POSTED_FOLDER_ID", "")

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    CAPTION_PROMPT = os.getenv(
        "CAPTION_PROMPT",
        "写真から読み取れる範囲で、自然で簡潔なInstagram投稿文を日本語で作成してください。",
    )

    INSTAGRAM_API_VERSION = os.getenv("INSTAGRAM_API_VERSION", "v26.0")
    INSTAGRAM_IG_USER_ID = os.getenv("INSTAGRAM_IG_USER_ID", "")
    INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    META_APP_ID = os.getenv("META_APP_ID", "").strip()
    META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()
    META_LOGIN_CONFIG_ID = os.getenv("META_LOGIN_CONFIG_ID", "").strip()
    # 未指定ならAPP_SECRET_KEYから専用の暗号鍵を導出する。
    ACCOUNT_TOKEN_ENCRYPTION_KEY = os.getenv("ACCOUNT_TOKEN_ENCRYPTION_KEY", "").strip()

    DATABASE_PATH = _path("DATABASE_PATH", "data/instagram.db")
    MEDIA_DIR = _path("MEDIA_DIR", "data/media")
    LOG_FILE = _path("LOG_FILE", "logs/app.log")
    MAX_SYNC_FILES = int(os.getenv("MAX_SYNC_FILES", "100"))
    APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Tokyo")
    DEFAULT_SCHEDULE_HOUR = int(os.getenv("DEFAULT_SCHEDULE_HOUR", "16"))
    PUBLISH_STALE_MINUTES = int(os.getenv("PUBLISH_STALE_MINUTES", "10"))

    @classmethod
    def account_profiles(cls):
        """管理画面で扱うInstagramアカウントを返す。

        既存の環境変数名は二九写真店用として残し、稼働中環境との互換性を
        維持する。2つ目以降はアカウント名を接頭辞にした環境変数を使う。
        """
        return {
            "29shashinten": AccountProfile(
                key="29shashinten",
                label=os.getenv("PRIMARY_ACCOUNT_LABEL", "二九写真店"),
                username=os.getenv("PRIMARY_INSTAGRAM_USERNAME", "29shashinten"),
                drive_inbox_folder_id=cls.DRIVE_INBOX_FOLDER_ID,
                drive_posted_folder_id=cls.DRIVE_POSTED_FOLDER_ID,
                instagram_ig_user_id=cls.INSTAGRAM_IG_USER_ID,
                instagram_access_token=cls.INSTAGRAM_ACCESS_TOKEN,
                caption_prompt=os.getenv("PRIMARY_CAPTION_PROMPT", cls.CAPTION_PROMPT),
            ),
            "crach29": AccountProfile(
                key="crach29",
                label=os.getenv("CRACH29_ACCOUNT_LABEL", "Crach29"),
                username=os.getenv("CRACH29_INSTAGRAM_USERNAME", "crach.29"),
                drive_inbox_folder_id=os.getenv(
                    "CRACH29_DRIVE_INBOX_FOLDER_ID",
                    "",
                ),
                drive_posted_folder_id=os.getenv(
                    "CRACH29_DRIVE_POSTED_FOLDER_ID",
                    "",
                ),
                instagram_ig_user_id=os.getenv("CRACH29_INSTAGRAM_IG_USER_ID", ""),
                instagram_access_token=os.getenv(
                    "CRACH29_INSTAGRAM_ACCESS_TOKEN", ""
                ),
                caption_prompt=os.getenv(
                    "CRACH29_CAPTION_PROMPT",
                    "写真から事実として読み取れる範囲で、Crach29のInstagram投稿文を"
                    "日本語で作成してください。過度に格好つけず、仕事や活動内容が伝わる"
                    "自然で簡潔な文章にしてください。ハッシュタグは本文末尾に5個だけ"
                    "付け、不明な固有名詞や撮影意図は作らないでください。",
                ),
            ),
            "bisyu": AccountProfile(
                key="bisyu",
                label=os.getenv("BISYU_ACCOUNT_LABEL", "美趣"),
                username=os.getenv("BISYU_INSTAGRAM_USERNAME", "bisyu_1995"),
                drive_inbox_folder_id=os.getenv(
                    "BISYU_DRIVE_INBOX_FOLDER_ID",
                    "",
                ),
                drive_posted_folder_id=os.getenv(
                    "BISYU_DRIVE_POSTED_FOLDER_ID",
                    "",
                ),
                instagram_ig_user_id=os.getenv("BISYU_INSTAGRAM_IG_USER_ID", ""),
                instagram_access_token=os.getenv(
                    "BISYU_INSTAGRAM_ACCESS_TOKEN", ""
                ),
                caption_prompt=os.getenv(
                    "BISYU_CAPTION_PROMPT",
                    "写真から事実として読み取れる範囲で、美趣のInstagram投稿文を"
                    "日本語で作成してください。落ち着きと親しみがあり、サービスや"
                    "店内の雰囲気が自然に伝わる簡潔な文章にしてください。"
                    "ハッシュタグは本文末尾に5個だけ付け、不明な固有名詞や効果を"
                    "作らないでください。",
                ),
            ),
        }
