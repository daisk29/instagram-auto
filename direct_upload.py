"""Logged-in browser uploads for the existing multi-account Instagram app.

Upload flow: browser -> private temporary file -> normalized JPEG -> Google
Drive account inbox -> existing photos table. No automatic Instagram posting.
"""

import logging
import os
import re
import secrets
import tempfile
import unicodedata
import warnings
from datetime import datetime, timezone
from pathlib import Path

from flask import jsonify, request
from googleapiclient.http import MediaFileUpload
from PIL import Image, UnidentifiedImageError

from drive_client import DriveClient
from image_prepare import prepare_instagram_image

LOG = logging.getLogger(__name__)
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
COPY_CHUNK = 1024 * 1024
MAX_PIXELS = 80_000_000


class UploadRejected(ValueError):
    """Safe validation error shown in the dashboard."""


def max_upload_bytes():
    try:
        size = int(os.getenv("DIRECT_UPLOAD_MAX_MB", "25"))
    except ValueError:
        size = 25
    return max(1, min(size, 30)) * 1024 * 1024


def filename_for_drive(original_name):
    # Do not use user-supplied filenames in the server filesystem.
    basename = str(original_name or "photo").replace("\\", "/").rsplit("/", 1)[-1]
    stem = unicodedata.normalize("NFKC", Path(basename).stem)
    stem = "".join(c for c in stem if c.isprintable() and c not in '/\\<>:"|?*')
    stem = stem.strip(" .")[:60] or "photo"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "{}-{}-{}.jpg".format(stem, stamp, secrets.token_hex(4))


def write_validated_source(stream, target, limit):
    total = 0
    with target.open("wb") as dest:
        while True:
            chunk = stream.read(COPY_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UploadRejected("1枚あたり{}MB以下の画像を選択してください".format(limit // (1024 * 1024)))
            dest.write(chunk)
    if total == 0:
        raise UploadRejected("画像ファイルが空です")
    with target.open("rb") as source:
        header = source.read(8)
    if not (header.startswith(b"\xff\xd8\xff") or header == b"\x89PNG\r\n\x1a\n"):
        raise UploadRejected("JPEGまたはPNG画像のみアップロードできます")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(target) as img:
                if img.format not in ("JPEG", "PNG"):
                    raise UploadRejected("JPEGまたはPNG画像のみアップロードできます")
                if img.width * img.height > MAX_PIXELS:
                    raise UploadRejected("画像の解像度が大きすぎます")
                img.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise UploadRejected("画像を読み込めません。別のJPEG/PNG画像を選択してください") from exc
    return total


def register_direct_upload_routes(app, config, database, account_profile, login_required, verify_csrf):
    """Register an isolated endpoint; no changes to the existing DB schema."""

    @app.post("/direct-upload/photo", endpoint="direct_upload_photo")
    @login_required
    def upload_photo():
        limit = max_upload_bytes()
        # Check before Werkzeug parses multipart data.
        if request.content_length and request.content_length > limit + 1024 * 1024:
            return jsonify(ok=False, error="ファイルの容量が大きすぎます"), 413

        verify_csrf()
        account = account_profile(request.form.get("account_key"))
        if not account.drive_inbox_folder_id:
            return jsonify(ok=False, error="選択中のアカウントにDrive受信フォルダがありません"), 400
        uploaded = request.files.get("photo")
        if uploaded is None or not uploaded.filename:
            return jsonify(ok=False, error="画像を選択してください"), 400

        folder_id = account.drive_inbox_folder_id
        media_dir = Path(config.MEDIA_DIR)
        media_dir.mkdir(parents=True, exist_ok=True)
        remote_id = None
        local_path = None
        service = None
        try:
            with tempfile.TemporaryDirectory(prefix=".direct-upload-", dir=str(media_dir)) as tmp:
                source = Path(tmp) / "source"
                normalized = Path(tmp) / "normalized.jpg"
                write_validated_source(uploaded.stream, source, limit)
                prepare_instagram_image(source, normalized)
                if not normalized.is_file() or normalized.stat().st_size == 0:
                    raise UploadRejected("画像の変換に失敗しました")
                if normalized.stat().st_size > limit:
                    raise UploadRejected("変換後の画像が容量上限を超えました")

                # Prepare exactly the JPEG that will be shown/published later.
                name = filename_for_drive(uploaded.filename)
                service = DriveClient(config, account).service()
                result = service.files().create(
                    body={"name": name, "mimeType": "image/jpeg", "parents": [folder_id]},
                    media_body=MediaFileUpload(str(normalized), mimetype="image/jpeg", resumable=False),
                    fields="id,name,mimeType,modifiedTime,size",
                    supportsAllDrives=True,
                ).execute()
                remote_id = result.get("id", "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", remote_id):
                    raise RuntimeError("Driveから不正なファイルIDが返されました")
                local_path = media_dir / (remote_id + ".jpg")
                os.replace(str(normalized), str(local_path))
                database.upsert_photo(
                    {
                        "id": remote_id,
                        "name": result.get("name") or name,
                        "mimeType": "image/jpeg",
                        "modifiedTime": result.get("modifiedTime"),
                    },
                    local_path,
                    secrets.token_urlsafe(32),
                    account.key,
                )
        except UploadRejected as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception:
            LOG.exception("Dashboard photo upload failed for account %s", account.key)
            # Only delete a remote object that was created by this request.
            if remote_id and service is not None:
                try:
                    service.files().delete(fileId=remote_id, supportsAllDrives=True).execute()
                except Exception:
                    LOG.exception("Could not roll back newly uploaded Drive file")
            if local_path is not None:
                try:
                    local_path.unlink(missing_ok=True)
                except OSError:
                    LOG.exception("Could not remove incomplete local upload")
            return jsonify(ok=False, error="アップロードに失敗しました。Driveの接続状態を確認してください"), 502
        return jsonify(ok=True, name=name, account=account.key, message="写真を未処理に追加しました")
