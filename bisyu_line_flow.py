"""美趣専用LINEのキャプション返信・承認・投稿。共用LINEとは独立。"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import secrets
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from line_drive_common import FOLDER_ID, allowed_users, get_setting

LOG = logging.getLogger(__name__)
ACCOUNT_KEY = "bisyu"
CAPTION_LIMIT = 2200
APPROVAL = re.compile(r"bisyu_approve:([1-9][0-9]{0,17}):([0-9a-f]{32})")
DRIVE_FILE_ID = re.compile(r"[A-Za-z0-9_-]{5,200}")


class FlowError(RuntimeError):
    """運用担当にLINEで表示してもよいエラー。"""


def enabled():
    return get_setting("BISYU_LINE_ENABLE_CAPTION_REPLY", "0") == "1"


def posting_enabled():
    return enabled() and get_setting("BISYU_LINE_ENABLE_POSTING", "0") == "1"


def can_approve(user_id):
    return user_id in allowed_users() and user_id in {
        part.strip() for part in get_setting("BISYU_LINE_APPROVER_IDS").split(",") if part.strip()
    }


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS bisyu_line_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        line_user_id TEXT NOT NULL,
        folder_id TEXT NOT NULL,
        message_ids TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued',
        nonce TEXT NOT NULL,
        draft_id INTEGER,
        preview_caption TEXT,
        ig_user_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        run_after INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS bisyu_line_outbox (
        retry_key TEXT PRIMARY KEY,
        line_user_id TEXT NOT NULL,
        messages TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        run_after INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )""")


def text_message(text):
    return {"type": "text", "text": text}


def queue_message(conn, user_id, *messages):
    conn.execute(
        "INSERT INTO bisyu_line_outbox(retry_key,line_user_id,messages,created_at) VALUES (?,?,?,?)",
        (str(uuid4()), user_id, json.dumps(messages, ensure_ascii=False), int(time.time())),
    )


def queue_proposal(conn, user_id, photos):
    """Called in the same transaction as the reference-note association."""
    initialize(conn)
    folder = get_setting("BISYU_DRIVE_INBOX_FOLDER_ID")
    ids = [row["message_id"] for row in photos if row["folder_id"] == folder]
    if not FOLDER_ID.fullmatch(folder) or not ids:
        return
    if len(ids) > 10:
        queue_message(conn, user_id, text_message(
            "写真は1投稿につき10枚までです。Google Driveに保存した写真は管理画面から選択できます。"
        ))
        return
    conn.execute(
        "INSERT INTO bisyu_line_proposals(line_user_id,folder_id,message_ids,nonce,created_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, folder, json.dumps(ids), secrets.token_hex(16), int(time.time())),
    )


def receive_approval(conn, user_id, event):
    """An approval is single-use and belongs to the user who sent the photos."""
    if not posting_enabled() or not can_approve(user_id):
        return False
    match = APPROVAL.fullmatch(str((event.get("postback") or {}).get("data", "")))
    if not match:
        return False
    initialize(conn)
    cur = conn.execute(
        "UPDATE bisyu_line_proposals SET state='approved' "
        "WHERE id=? AND nonce=? AND line_user_id=? AND state='ready'",
        (int(match[1]), match[2], user_id),
    )
    conn.commit()
    return cur.rowcount == 1


def proposal_messages(proposal, caption, count, draft_id):
    messages = [text_message(f"美趣のキャプション案（写真{count}枚・下書き #{draft_id}）\n\n{caption}")]
    if posting_enabled() and can_approve(proposal["line_user_id"]):
        messages.append({
            "type": "template",
            "altText": "美趣のInstagram投稿を承認",
            "template": {
                "type": "buttons",
                "text": f"下書き #{draft_id} を @bisyu_1995 に公開しますか？",
                "actions": [{
                    "type": "postback", "label": "承認して今すぐ投稿",
                    "data": f"bisyu_approve:{proposal['id']}:{proposal['nonce']}",
                    "displayText": f"美趣の下書き #{draft_id} を承認",
                }],
            },
        })
    else:
        messages.append(text_message("LINEからの投稿は利用できません。必要に応じて管理画面で確認してください。"))
    return messages


def account_for(proposal, accounts):
    account = accounts.get(ACCOUNT_KEY)
    if not account or account.username.casefold() != "bisyu_1995":
        raise FlowError("美趣のInstagramアカウント設定を確認してください。")
    if (proposal["folder_id"] != get_setting("BISYU_DRIVE_INBOX_FOLDER_ID")
            or proposal["folder_id"] != account.drive_inbox_folder_id):
        raise FlowError("美趣のLINEと管理画面でGoogle Driveの受信フォルダが一致していません。")
    return account


def jobs_for(conn, proposal):
    ids = json.loads(proposal["message_ids"])
    marks = ",".join("?" for _ in ids)
    rows = {row["message_id"]: row for row in conn.execute(
        f"SELECT * FROM jobs WHERE message_id IN ({marks}) AND line_user_id=? AND folder_id=?",
        (*ids, proposal["line_user_id"], proposal["folder_id"]),
    )}
    if any(mid not in rows or rows[mid]["status"] == "failed" for mid in ids):
        raise FlowError("写真のDrive保存を確認できません。管理画面を確認してください。")
    if any(rows[mid]["status"] != "done" or not rows[mid]["google_file_id"] for mid in ids):
        return None
    return [rows[mid] for mid in ids]


def import_photos(database, config, account, jobs):
    from database import utcnow
    from drive_client import DriveClient
    from googleapiclient.http import MediaIoBaseDownload
    from image_prepare import prepare_instagram_image

    drive = DriveClient(config, account)
    service = None
    photo_ids = []
    for job in jobs:
        file_id = job["google_file_id"]
        if not DRIVE_FILE_ID.fullmatch(file_id):
            raise FlowError("Driveの写真を確認できません。")
        with database.connect() as db_conn:
            photo = db_conn.execute(
                "SELECT id,account_key,status,local_path FROM photos WHERE drive_file_id=?", (file_id,)
            ).fetchone()
        if photo:
            if photo["account_key"] != ACCOUNT_KEY or photo["status"] != "inbox":
                raise FlowError("すでに別の下書きに使われた写真があります。管理画面を確認してください。")
            if Path(photo["local_path"]).is_file():
                photo_ids.append(photo["id"])
                continue
        if service is None:
            service = drive.service()
        item = service.files().get(
            fileId=file_id, fields="id,name,mimeType,modifiedTime,parents,trashed",
            supportsAllDrives=True,
        ).execute()
        if (item.get("trashed") or account.drive_inbox_folder_id not in item.get("parents", [])
                or item.get("id") != file_id):
            raise FlowError("美趣の受信フォルダから写真が移動されています。")
        if item.get("mimeType") not in {"image/jpeg", "image/png", "image/webp"}:
            raise FlowError("JPEG・PNG・WebPの写真を送ってください。")
        config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        target = config.MEDIA_DIR / f"{file_id}.jpg"
        with TemporaryDirectory(dir=config.MEDIA_DIR) as temp:
            source = Path(temp) / "source"
            output = Path(temp) / "image.jpg"
            request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
            with source.open("wb") as handle:
                downloader = MediaIoBaseDownload(handle, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            prepare_instagram_image(source, output)
            os.replace(output, target)
        item["mimeType"] = "image/jpeg"
        with database.connect() as db_conn:
            now = utcnow()
            # If dashboard sync imported this concurrently, do not reassign the photo.
            db_conn.execute(
                "INSERT INTO photos(drive_file_id,name,mime_type,modified_time,local_path,"
                "public_token,account_key,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,'inbox',?,?) ON CONFLICT(drive_file_id) DO NOTHING",
                (file_id, item["name"], item["mimeType"], item.get("modifiedTime"),
                 str(target), secrets.token_urlsafe(32), ACCOUNT_KEY, now, now),
            )
            photo = db_conn.execute(
                "SELECT id,account_key,status,local_path FROM photos WHERE drive_file_id=?", (file_id,)
            ).fetchone()
            if photo["account_key"] != ACCOUNT_KEY or photo["status"] != "inbox":
                raise FlowError("すでに別の下書きに使われた写真があります。管理画面を確認してください。")
            if not Path(photo["local_path"]).is_file():
                db_conn.execute(
                    "UPDATE photos SET local_path=?,mime_type='image/jpeg',updated_at=? WHERE id=?",
                    (str(target), now, photo["id"]),
                )
        photo_ids.append(photo["id"])
    return photo_ids


def create_proposal(conn, proposal, config, database, accounts):
    from caption_client import CaptionClient

    account = account_for(proposal, accounts)
    jobs = jobs_for(conn, proposal)
    if jobs is None:
        return
    source = f"bisyu-line:{proposal['nonce']}"
    existing = database.draft_for_source(source)
    if existing is not None:
        draft = database.get_draft(existing["draft_id"])
    else:
        photo_ids = import_photos(database, config, account, jobs)
        photos = database.get_photos(photo_ids)
        if len(photos) != len(photo_ids):
            raise FlowError("下書きに使う写真を取得できませんでした。")
        caption = CaptionClient(config, account).generate(photos)
        caption = caption[:CAPTION_LIMIT].strip()
        if not caption:
            raise RuntimeError("Generated caption is empty")
        draft_id = database.create_draft(photo_ids, caption, ACCOUNT_KEY, source_key=source)
        draft = database.get_draft(draft_id)
    if (not draft or draft["account_key"] != ACCOUNT_KEY or draft["status"] != "draft"
            or not 0 < len(draft["caption"]) <= CAPTION_LIMIT):
        raise FlowError("下書きの状態が変わりました。管理画面で確認してください。")
    conn.execute(
        "UPDATE bisyu_line_proposals SET state='ready',draft_id=?,preview_caption=?,ig_user_id=? WHERE id=?",
        (draft["id"], draft["caption"], account.instagram_ig_user_id, proposal["id"]),
    )
    queue_message(conn, proposal["line_user_id"], *proposal_messages(
        proposal, draft["caption"], len(jobs), draft["id"],
    ))
    conn.commit()


def finish(conn, proposal, state, message):
    conn.execute("UPDATE bisyu_line_proposals SET state=? WHERE id=?", (state, proposal["id"]))
    queue_message(conn, proposal["line_user_id"], text_message(message))
    conn.commit()


def publish(conn, proposal, config, database, accounts):
    from publish_service import publish_claimed_draft

    if not posting_enabled() or not can_approve(proposal["line_user_id"]):
        raise FlowError("LINEからの投稿は無効になりました。管理画面で確認してください。")
    account = account_for(proposal, accounts)
    if (not proposal["ig_user_id"] or account.instagram_ig_user_id != proposal["ig_user_id"]
            or not account.instagram_access_token
            or not account.drive_posted_folder_id or not config.PUBLIC_BASE_URL.startswith("https://")):
        raise FlowError("Instagram接続・投稿済みフォルダ・公開URLの設定を確認してください。")
    # Persist intent before claiming. If interrupted here, report for manual review,
    # never replay a publish request whose outcome could be ambiguous.
    conn.execute("UPDATE bisyu_line_proposals SET state='publishing' WHERE id=?", (proposal["id"],))
    conn.commit()
    if not database.claim_publish(proposal["draft_id"], proposal["preview_caption"],
                                  expected_account_key=ACCOUNT_KEY):
        raise FlowError("下書きが変更されたか、すでに処理されています。管理画面で確認してください。")
    # Pin the account used for this call to the IG id displayed with the approval.
    publish_claimed_draft(database, config, {ACCOUNT_KEY: account}, proposal["draft_id"])


def sync_results(conn, database):
    for proposal in conn.execute("SELECT * FROM bisyu_line_proposals WHERE state='publishing'").fetchall():
        draft = database.get_draft(proposal["draft_id"])
        if draft and draft["account_key"] == ACCOUNT_KEY:
            if draft["status"] in ("publishing", "committing"):
                continue
            if draft["status"] == "posted":
                message = "美趣のInstagramに投稿しました。"
                if draft["permalink"]:
                    message += "\n" + draft["permalink"]
                if draft["error_message"]:
                    message += "\nGoogle Driveの移動状況は管理画面で確認してください。"
                finish(conn, proposal, "posted", message)
                continue
        finish(conn, proposal, "failed",
               "投稿結果の確認が必要です。再投稿する前に管理画面とInstagramを確認してください。")


def send_outbox(conn):
    import requests

    token = get_setting("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        return
    now = int(time.time())
    for row in conn.execute(
        "SELECT * FROM bisyu_line_outbox WHERE run_after<=? ORDER BY rowid LIMIT 20", (now,)
    ).fetchall():
        if row["line_user_id"] not in allowed_users() or row["attempts"] >= 8 or now - row["created_at"] >= 86400:
            conn.execute("DELETE FROM bisyu_line_outbox WHERE retry_key=?", (row["retry_key"],))
        else:
            status = 0
            try:
                with requests.post(
                    "https://api.line.me/v2/bot/message/push",
                    headers={"Authorization": f"Bearer {token}", "X-Line-Retry-Key": row["retry_key"]},
                    json={"to": row["line_user_id"], "messages": json.loads(row["messages"])},
                    timeout=(5, 15),
                ) as response:
                    status = response.status_code
                    accepted = 200 <= status < 300 or (
                        status == 409 and bool(response.headers.get("X-Line-Accepted-Request-Id"))
                    )
            except requests.RequestException:
                accepted = False
            if accepted:
                conn.execute("DELETE FROM bisyu_line_outbox WHERE retry_key=?", (row["retry_key"],))
            else:
                LOG.warning("BISYU LINE push failed (HTTP %s); retry scheduled", status or "network")
                conn.execute(
                    "UPDATE bisyu_line_outbox SET attempts=attempts+1,run_after=? WHERE retry_key=?",
                    (now + min(3600, 60 * 2 ** row["attempts"]), row["retry_key"]),
                )
        conn.commit()


def process(conn, config, database, accounts):
    initialize(conn)
    # Approvals should not wait behind AI generation.
    rows = conn.execute(
        "SELECT * FROM bisyu_line_proposals WHERE state IN ('approved','queued') AND run_after<=? "
        "ORDER BY state,run_after,id LIMIT 10", (int(time.time()),),
    ).fetchall()
    for proposal in rows:
        try:
            if proposal["state"] == "approved":
                publish(conn, proposal, config, database, accounts)
            elif proposal["line_user_id"] in allowed_users():
                create_proposal(conn, proposal, config, database, accounts)
                conn.execute(
                    "UPDATE bisyu_line_proposals SET run_after=? WHERE id=? AND state='queued'",
                    (int(time.time()) + 15, proposal["id"]),
                )
                conn.commit()
            else:
                conn.execute("UPDATE bisyu_line_proposals SET state='failed' WHERE id=?", (proposal["id"],))
                conn.commit()
        except FlowError as exc:
            conn.rollback()
            finish(conn, proposal, "failed", str(exc))
        except Exception:
            conn.rollback()
            LOG.exception("BISYU proposal %s failed", proposal["id"])
            if proposal["state"] == "queued":
                attempt = proposal["attempts"] + 1
                if attempt >= 4:
                    finish(conn, proposal, "failed",
                           "キャプション案を作れませんでした。写真はGoogle Driveに保存されています。管理画面を確認してください。")
                else:
                    conn.execute("UPDATE bisyu_line_proposals SET attempts=?,run_after=? WHERE id=?",
                                 (attempt, int(time.time()) + 60 * 2 ** (attempt - 1), proposal["id"]))
                    conn.commit()
            else:
                state = conn.execute("SELECT state FROM bisyu_line_proposals WHERE id=?",
                                     (proposal["id"],)).fetchone()["state"]
                if state != "publishing":
                    finish(conn, proposal, "failed", "投稿を開始できませんでした。管理画面を確認してください。")
    sync_results(conn, database)
    send_outbox(conn)


def run(conn):
    from account_store import AccountRegistry, AccountStore
    from config import Config
    from database import Database

    initialize(conn)
    if not conn.execute(
        "SELECT 1 FROM bisyu_line_proposals WHERE state IN ('approved','queued','publishing') LIMIT 1"
    ).fetchone():
        send_outbox(conn)
        return
    Config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    database = Database(Config.DATABASE_PATH)
    database.init()
    process(conn, Config, database, AccountRegistry(AccountStore(Config, database)))
