import os
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from image_prepare import prepare_instagram_image

SCOPES = ["https://www.googleapis.com/auth/drive"]
IMAGE_MIMES = {"image/jpeg", "image/png"}


class DriveClient:
    def __init__(self, config, account=None):
        self.config = config
        self.account = account
        self._service = None

    def credentials(self, interactive=False):
        token_path = self.config.GOOGLE_TOKEN_FILE
        creds = None
        if token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            except (ValueError, OSError):
                if not interactive:
                    raise RuntimeError(
                        "Google Driveのtoken.jsonを読み込めません。Macでcreate_token.pyを再実行してください"
                    ) from None
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                if not interactive:
                    raise RuntimeError(
                        "Google Driveの認証が失効しました。Macでcreate_token.pyを再実行し、"
                        "新しいtoken.jsonをサーバーへ送ってください"
                    ) from None
                creds = None
            else:
                self._save_token(token_path, creds.to_json())
        if (not creds or not creds.valid) and interactive:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.config.GOOGLE_CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
            self._save_token(token_path, creds.to_json())
        if not creds or not creds.valid:
            raise RuntimeError("Google Driveのtoken.jsonが未設定または無効です")
        return creds

    @staticmethod
    def _save_token(path, contents):
        """更新中に停止しても認証ファイルを壊さず、所有者だけに読ませる。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".token-",
            delete=False,
        ) as temporary:
            temporary.write(contents)
            temporary_path = Path(temporary.name)
        try:
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def service(self):
        if self._service is None:
            self._service = build(
                "drive", "v3", credentials=self.credentials(), cache_discovery=False
            )
        return self._service

    def sync(self, database):
        folder_id = (
            self.account.drive_inbox_folder_id
            if self.account else self.config.DRIVE_INBOX_FOLDER_ID
        )
        if not folder_id:
            raise RuntimeError("DRIVE_INBOX_FOLDER_IDが未設定です")
        service = self.service()
        imported = 0
        skipped = 0
        account_key = self.account.key if self.account else "29shashinten"
        existing = {row["drive_file_id"] for row in database.list_photos()}
        limit = max(1, self.config.MAX_SYNC_FILES)
        page_token = None
        while imported < limit:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "pageSize": min(1000, limit),
                "orderBy": "createdTime asc",
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size)",
            }
            if page_token:
                params["pageToken"] = page_token
            response = service.files().list(**params).execute()
            for item in response.get("files", []):
                if item["mimeType"] not in IMAGE_MIMES or item["id"] in existing:
                    skipped += 1
                    continue
                local_path = self.config.MEDIA_DIR / f"{item['id']}.jpg"
                with NamedTemporaryFile(suffix=".source", delete=True) as source:
                    self._download(service, item["id"], Path(source.name))
                    prepare_instagram_image(source.name, local_path)
                normalized = dict(item)
                normalized["mimeType"] = "image/jpeg"
                database.upsert_photo(
                    normalized, local_path, secrets.token_urlsafe(32), account_key
                )
                existing.add(item["id"])
                imported += 1
                if imported >= limit:
                    break
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return imported, skipped

    @staticmethod
    def _download(service, file_id, target: Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        request = service.files().get_media(fileId=file_id)
        with target.open("wb") as output:
            downloader = MediaIoBaseDownload(output, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

    def move_to_posted(self, drive_file_id):
        posted_folder_id = (
            self.account.drive_posted_folder_id
            if self.account else self.config.DRIVE_POSTED_FOLDER_ID
        )
        if not posted_folder_id:
            raise RuntimeError("Drive postedフォルダIDが未設定です")
        service = self.service()
        metadata = service.files().get(fileId=drive_file_id, fields="parents").execute()
        parents = ",".join(metadata.get("parents", []))
        service.files().update(
            fileId=drive_file_id,
            addParents=posted_folder_id,
            removeParents=parents,
            fields="id,parents",
        ).execute()
