import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drive_file_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    modified_time TEXT,
                    local_path TEXT NOT NULL,
                    public_token TEXT NOT NULL UNIQUE,
                    account_key TEXT NOT NULL DEFAULT '29shashinten',
                    status TEXT NOT NULL DEFAULT 'inbox',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    photo_ids TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    account_key TEXT NOT NULL DEFAULT '29shashinten',
                    status TEXT NOT NULL DEFAULT 'draft',
                    instagram_media_id TEXT,
                    permalink TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    scheduled_at TEXT,
                    posted_at TEXT,
                    progress INTEGER NOT NULL DEFAULT 0,
                    progress_message TEXT,
                    instagram_creation_id TEXT,
                    publish_started_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER,
                    event_type TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(draft_id) REFERENCES drafts(id)
                );
                CREATE TABLE IF NOT EXISTS draft_sources (
                    source_key TEXT PRIMARY KEY,
                    draft_id INTEGER UNIQUE REFERENCES drafts(id) ON DELETE SET NULL
                );
                """
            )
            # 既存運用中DBへ列を安全に追加する。既存データは二九写真店に属する。
            photo_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(photos)")
            }
            if "account_key" not in photo_columns:
                conn.execute(
                    "ALTER TABLE photos ADD COLUMN account_key TEXT NOT NULL "
                    "DEFAULT '29shashinten'"
                )
            draft_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(drafts)")
            }
            if "account_key" not in draft_columns:
                conn.execute(
                    "ALTER TABLE drafts ADD COLUMN account_key TEXT NOT NULL "
                    "DEFAULT '29shashinten'"
                )
            if "progress" not in draft_columns:
                conn.execute(
                    "ALTER TABLE drafts ADD COLUMN progress INTEGER NOT NULL DEFAULT 0"
                )
            if "progress_message" not in draft_columns:
                conn.execute("ALTER TABLE drafts ADD COLUMN progress_message TEXT")
            if "scheduled_at" not in draft_columns:
                conn.execute("ALTER TABLE drafts ADD COLUMN scheduled_at TEXT")
            if "instagram_creation_id" not in draft_columns:
                conn.execute("ALTER TABLE drafts ADD COLUMN instagram_creation_id TEXT")
            if "publish_started_at" not in draft_columns:
                conn.execute("ALTER TABLE drafts ADD COLUMN publish_started_at TEXT")

    def upsert_photo(
        self, item, local_path, public_token, account_key="29shashinten"
    ):
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO photos
                    (drive_file_id, name, mime_type, modified_time, local_path,
                     public_token, account_key, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'inbox', ?, ?)
                ON CONFLICT(drive_file_id) DO UPDATE SET
                    name=excluded.name,
                    mime_type=excluded.mime_type,
                    modified_time=excluded.modified_time,
                    local_path=excluded.local_path,
                    account_key=excluded.account_key,
                    updated_at=excluded.updated_at
                """,
                (
                    item["id"], item["name"], item["mimeType"],
                    item.get("modifiedTime"), str(local_path), public_token,
                    account_key, now, now,
                ),
            )

    def list_photos(self, status=None, account_key=None):
        query = "SELECT * FROM photos"
        params = []
        conditions = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if account_key:
            conditions.append("account_key = ?")
            params.append(account_key)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY COALESCE(modified_time, created_at), id"
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def get_photos(self, photo_ids):
        if not photo_ids:
            return []
        marks = ",".join("?" for _ in photo_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM photos WHERE id IN ({marks})", photo_ids
            ).fetchall()
        by_id = {row["id"]: row for row in rows}
        return [by_id[i] for i in photo_ids if i in by_id]

    def photo_by_token(self, token):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM photos WHERE public_token = ?", (token,)
            ).fetchone()

    def draft_for_source(self, source_key):
        with self.connect() as conn:
            return conn.execute(
                "SELECT draft_id FROM draft_sources WHERE source_key=?", (source_key,)
            ).fetchone()

    def create_draft(self, photo_ids, caption, account_key="29shashinten", *, source_key=None):
        if not 1 <= len(photo_ids) <= 10 or len(set(photo_ids)) != len(photo_ids):
            raise ValueError("写真を1〜10枚、重複せずに選択してください")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if source_key is not None:
                source = conn.execute(
                    "SELECT draft_id FROM draft_sources WHERE source_key=?", (source_key,)
                ).fetchone()
                if source is not None:
                    if source["draft_id"] is None:
                        raise ValueError("元の投稿案は削除されています")
                    return source["draft_id"]
            marks = ",".join("?" for _ in photo_ids)
            available = conn.execute(
                f"SELECT COUNT(*) FROM photos WHERE id IN ({marks}) "
                "AND account_key=? AND status='inbox'",
                [*photo_ids, account_key],
            ).fetchone()[0]
            if available != len(photo_ids):
                raise ValueError("選択した写真の状態を確認してください")
            cur = conn.execute(
                "INSERT INTO drafts(photo_ids, caption, account_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (json.dumps(photo_ids), caption, account_key, now, now),
            )
            draft_id = cur.lastrowid
            if source_key is not None:
                conn.execute(
                    "INSERT INTO draft_sources(source_key,draft_id) VALUES (?,?)",
                    (source_key, draft_id),
                )
            conn.execute(
                f"UPDATE photos SET status='review', updated_at=? WHERE id IN ({marks})",
                [now, *photo_ids],
            )
        self.event(draft_id, "caption_generated")
        return draft_id

    def list_drafts(self, account_key=None):
        with self.connect() as conn:
            if account_key:
                return conn.execute(
                    "SELECT * FROM drafts WHERE account_key = ? ORDER BY id DESC",
                    (account_key,),
                ).fetchall()
            return conn.execute("SELECT * FROM drafts ORDER BY id DESC").fetchall()

    def get_draft(self, draft_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()

    def update_draft(self, draft_id, **fields):
        allowed = {
            "caption", "status", "instagram_media_id", "permalink",
            "error_message", "scheduled_at", "posted_at", "progress",
            "progress_message",
            "instagram_creation_id", "publish_started_at",
        }
        values = {k: v for k, v in fields.items() if k in allowed}
        values["updated_at"] = utcnow()
        sql = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE drafts SET {sql} WHERE id = ?",
                [*values.values(), draft_id],
            )

    @staticmethod
    def _set_photos_for_draft(conn, draft_id, status, now):
        row = conn.execute(
            "SELECT photo_ids FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        photo_ids = json.loads(row["photo_ids"])
        if photo_ids:
            marks = ",".join("?" for _ in photo_ids)
            conn.execute(
                f"UPDATE photos SET status=?, updated_at=? WHERE id IN ({marks})",
                [status, now, *photo_ids],
            )

    def update_editable_draft(self, draft_id, caption, status=None):
        """画面が古くても投稿中・投稿済みの内容を上書きしない。"""
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE drafts SET caption=?, status=COALESCE(?, status), "
                "error_message=NULL, updated_at=? WHERE id=? "
                "AND status IN ('draft', 'hold', 'error', 'cancelled')",
                (caption, status, now, draft_id),
            )
            if cur.rowcount and status:
                self._set_photos_for_draft(
                    conn, draft_id, "hold" if status == "hold" else "review", now
                )
            return cur.rowcount == 1

    def claim_publish(self, draft_id, caption, *, expected_account_key=None):
        """二重クリックや並行リクエストによる二重投稿を防ぐ。"""
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE drafts
                SET caption=?, status='publishing', error_message=NULL,
                    scheduled_at=NULL, progress=5,
                    progress_message='投稿処理を開始しています',
                    instagram_creation_id=NULL, instagram_media_id=NULL,
                    permalink=NULL, posted_at=NULL, publish_started_at=?, updated_at=?
                WHERE id=? AND status IN ('draft', 'hold', 'error', 'cancelled')
                  AND (? IS NULL OR (account_key=? AND caption=? AND status='draft'))
                """,
                (caption, now, now, draft_id, expected_account_key, expected_account_key, caption),
            )
            if cur.rowcount:
                self._set_photos_for_draft(conn, draft_id, "review", now)
            return cur.rowcount == 1

    def schedule_draft(self, draft_id, caption, scheduled_at):
        """投稿案を予約する。投稿済み・処理中のレコードは変更しない。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow()
            cur = conn.execute(
                """
                UPDATE drafts
                SET caption=?, status='scheduled', scheduled_at=?,
                    error_message=NULL, progress=0,
                    progress_message='予約時刻まで待機しています', updated_at=?
                WHERE id=? AND status IN ('draft', 'hold', 'error', 'cancelled')
                """,
                (caption, scheduled_at, now, draft_id),
            )
            if cur.rowcount:
                self._set_photos_for_draft(conn, draft_id, "scheduled", now)
            return cur.rowcount == 1

    def cancel_schedule(self, draft_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow()
            cur = conn.execute(
                """
                UPDATE drafts
                SET status='draft', scheduled_at=NULL, progress=0,
                    progress_message=NULL, updated_at=?
                WHERE id=? AND status='scheduled'
                """,
                (now, draft_id),
            )
            if cur.rowcount:
                self._set_photos_for_draft(conn, draft_id, "review", now)
            return cur.rowcount == 1

    def list_due_drafts(self, due_at):
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM drafts
                WHERE status='scheduled' AND scheduled_at IS NOT NULL
                  AND scheduled_at <= ?
                ORDER BY scheduled_at, id
                """,
                (due_at,),
            ).fetchall()

    def claim_scheduled_publish(self, draft_id, due_at):
        """期限到来した予約を1プロセスだけが取得する。"""
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE drafts
                SET status='publishing', error_message=NULL, progress=5,
                    progress_message='予約投稿を開始しています',
                    instagram_creation_id=NULL, instagram_media_id=NULL,
                    permalink=NULL, posted_at=NULL, publish_started_at=?, updated_at=?
                WHERE id=? AND status='scheduled' AND scheduled_at <= ?
                """,
                (now, now, draft_id, due_at),
            )
            return cur.rowcount == 1

    def list_stale_publishing(self, cutoff):
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM drafts
                WHERE status IN ('publishing', 'committing') AND updated_at <= ?
                ORDER BY updated_at, id
                """,
                (cutoff,),
            ).fetchall()

    def claim_stale_recovery(self, draft_id, cutoff):
        """停止した処理を1プロセスだけが点検できるようにする。"""
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE drafts
                SET progress_message='中断した投稿処理を確認しています', updated_at=?
                WHERE id=? AND status IN ('publishing', 'committing')
                  AND updated_at <= ?
                """,
                (utcnow(), draft_id, cutoff),
            )
            return cur.rowcount == 1

    def begin_instagram_publish(self, draft_id):
        """中止可能な準備段階から、公開確定段階へ原子的に移行する。"""
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE drafts
                SET status='committing', progress=70,
                    progress_message='Instagramで投稿を公開しています',
                    updated_at=?
                WHERE id=? AND status='publishing'
                """,
                (utcnow(), draft_id),
            )
            return cur.rowcount == 1

    def cancel_publish(self, draft_id):
        """Instagram公開前の処理だけを中止する。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow()
            cur = conn.execute(
                """
                UPDATE drafts
                SET status='cancelled', error_message=NULL, progress=100,
                    progress_message='投稿処理を中止しました',
                    publish_started_at=NULL, updated_at=?
                WHERE id=? AND status='publishing'
                  AND instagram_media_id IS NULL
                """,
                (now, draft_id),
            )
            if cur.rowcount:
                self._set_photos_for_draft(conn, draft_id, "review", now)
            return cur.rowcount == 1

    def delete_inactive_draft(self, draft_id):
        """保留・中止の投稿案を削除し、使っていた写真を未処理へ戻す。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            draft = conn.execute(
                "SELECT photo_ids FROM drafts "
                "WHERE id=? AND status IN ('hold', 'cancelled')",
                (draft_id,),
            ).fetchone()
            if not draft:
                return False

            photo_ids = json.loads(draft["photo_ids"])
            conn.execute("DELETE FROM events WHERE draft_id=?", (draft_id,))
            cur = conn.execute(
                "DELETE FROM drafts WHERE id=? AND status IN ('hold', 'cancelled')",
                (draft_id,),
            )
            if cur.rowcount != 1:
                return False
            if photo_ids:
                marks = ",".join("?" for _ in photo_ids)
                conn.execute(
                    f"UPDATE photos SET status='inbox', updated_at=? "
                    f"WHERE id IN ({marks})",
                    [utcnow(), *photo_ids],
                )
            return True

    def reset_interrupted_publish(self, draft_id, message):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow()
            cur = conn.execute(
                """
                UPDATE drafts
                SET status='error', error_message=?, progress=100,
                    progress_message='処理が中断されました。再実行できます',
                    instagram_creation_id=NULL, publish_started_at=NULL,
                    updated_at=?
                WHERE id=? AND status IN ('publishing', 'committing')
                """,
                (message, now, draft_id),
            )
            if cur.rowcount:
                self._set_photos_for_draft(conn, draft_id, "review", now)
            return cur.rowcount == 1

    def mark_publish_unverified(self, draft_id):
        """公開結果が不明な場合、確認なしの再投稿を禁止する。"""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE drafts SET status='unverified', progress=100, "
                "progress_message='Instagramで公開結果を確認してください', "
                "error_message='公開結果を自動判定できませんでした。Instagramを確認するまで再投稿しないでください。', "
                "updated_at=? WHERE id=? AND status IN ('publishing', 'committing')",
                (utcnow(), draft_id),
            )
            return cur.rowcount == 1

    def confirm_unpublished(self, draft_id):
        """利用者がInstagramで未投稿を確認したときだけ再試行を許可する。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow()
            cur = conn.execute(
                "UPDATE drafts SET status='error', error_message=NULL, progress=0, "
                "progress_message='未投稿を確認しました。再実行できます', "
                "instagram_creation_id=NULL, publish_started_at=NULL, "
                "updated_at=? WHERE id=? AND status='unverified'",
                (now, draft_id),
            )
            if cur.rowcount:
                self._set_photos_for_draft(conn, draft_id, "review", now)
            return cur.rowcount == 1

    def update_publish_progress(self, draft_id, **fields):
        """中止・復旧済みの投稿へ遅れて届いた進捗を反映しない。"""
        allowed = {"progress", "progress_message", "instagram_creation_id"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return False
        values["updated_at"] = utcnow()
        sql = ", ".join(f"{key}=?" for key in values)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE drafts SET {sql} WHERE id=? "
                "AND status IN ('publishing', 'committing')",
                [*values.values(), draft_id],
            )
            return cur.rowcount == 1

    def heartbeat_publish(self, draft_id):
        """API待機中も稼働中の処理を中断扱いしない。"""
        with self.connect() as conn:
            conn.execute(
                "UPDATE drafts SET updated_at=? WHERE id=? "
                "AND status IN ('publishing', 'committing')",
                (utcnow(), draft_id),
            )

    def record_published(self, draft_id, media_id):
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE drafts SET instagram_media_id=?, progress=80, "
                "progress_message='Instagramへの公開が完了しました', updated_at=? "
                "WHERE id=? AND status='committing'",
                (media_id, utcnow(), draft_id),
            )
            return cur.rowcount == 1

    def finish_publish(self, draft_id, media_id, permalink):
        """公開確定と写真の状態変更を同じトランザクションで行う。"""
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE drafts SET status='posted', instagram_media_id=?, "
                "permalink=?, posted_at=?, error_message=NULL, progress=90, "
                "progress_message='Driveのpostedへ移動しています', "
                "publish_started_at=NULL, updated_at=? WHERE id=? "
                "AND status IN ('publishing', 'committing')",
                (media_id, permalink, now, now, draft_id),
            )
            if cur.rowcount:
                self._set_photos_for_draft(conn, draft_id, "posted", now)
            return cur.rowcount == 1

    def fail_publish(self, draft_id, message):
        """中止・投稿済みなど別処理の状態を失敗で上書きしない。"""
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE drafts SET status='error', error_message=?, progress=100, "
                "progress_message='投稿に失敗しました', publish_started_at=NULL, "
                "updated_at=? WHERE id=? AND status IN ('publishing', 'committing')",
                (message, now, draft_id),
            )
            if cur.rowcount:
                self._set_photos_for_draft(conn, draft_id, "review", now)
            return cur.rowcount == 1

    def mark_photos(self, photo_ids, status):
        if not photo_ids:
            return
        marks = ",".join("?" for _ in photo_ids)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE photos SET status=?, updated_at=? WHERE id IN ({marks})",
                [status, utcnow(), *photo_ids],
            )

    def event(self, draft_id, event_type, detail=None):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(draft_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
                (draft_id, event_type, detail, utcnow()),
            )
