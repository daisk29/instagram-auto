import json
import logging
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from drive_client import DriveClient
from instagram_client import InstagramClient


class PublishCancelled(RuntimeError):
    pass


def _finish_published(database, config, account, draft, photos, media_id, permalink=""):
    """Instagram成功を確定し、Drive整理を行う。再実行しても再投稿しない。"""
    draft_id = draft["id"]
    if not database.finish_publish(
        draft_id, media_id, permalink or draft["permalink"] or ""
    ):
        current = database.get_draft(draft_id)
        if current and current["status"] == "posted":
            return {"media_id": current["instagram_media_id"],
                    "permalink": current["permalink"], "warning": current["error_message"]}
        raise RuntimeError("投稿状態が変更されたため公開結果を確定できませんでした")

    move_errors = []
    drive_client = DriveClient(config, account)
    for photo in photos:
        try:
            drive_client.move_to_posted(photo["drive_file_id"])
        except Exception as exc:
            move_errors.append(f"{photo['name']}: {exc}")
            logging.exception("Drive move after publish failed")

    warning = None
    if move_errors:
        warning = (
            "Instagram投稿は成功しましたが、Drive移動に失敗しました: "
            + " / ".join(move_errors)
        )
        database.update_draft(
            draft_id, error_message=warning, progress=100,
            progress_message="投稿完了（Drive移動エラー）",
        )
        database.event(draft_id, "drive_move_failed", warning[:1000])
    else:
        database.update_draft(
            draft_id, progress=100, progress_message="投稿が完了しました"
        )
    database.event(draft_id, "posted", media_id)
    return {"media_id": media_id, "permalink": permalink, "warning": warning}


def publish_claimed_draft(database, config, accounts, draft_id):
    """publishing状態の投稿案をInstagramへ送信し、Driveを整理する。"""
    draft = database.get_draft(draft_id)
    if not draft or draft["status"] != "publishing":
        raise RuntimeError("投稿対象を取得できませんでした")

    photo_ids = []
    try:
        account = accounts.get(draft["account_key"])
        if not account:
            raise RuntimeError("投稿先アカウントの設定が見つかりません")

        photo_ids = json.loads(draft["photo_ids"])
        photos = database.get_photos(photo_ids)
        if not photos or len(photos) != len(photo_ids):
            raise RuntimeError("投稿する写真を取得できませんでした")

        def report_progress(percent, message):
            if not database.update_publish_progress(
                draft_id, progress=percent, progress_message=message
            ):
                check_cancelled()
                raise RuntimeError("投稿状態が変更されました")

        def check_cancelled():
            if database.get_draft(draft_id)["status"] == "cancelled":
                raise PublishCancelled("投稿処理が中止されました")

        def remember_container(creation_id):
            check_cancelled()
            if not database.update_publish_progress(
                draft_id, instagram_creation_id=creation_id, progress=40,
                progress_message="Instagram側で画像を処理しています",
            ):
                check_cancelled()
                raise RuntimeError("投稿状態が変更されました")

        def remember_publish_start():
            if not database.begin_instagram_publish(draft_id):
                check_cancelled()
                raise RuntimeError("投稿状態を公開処理へ移行できませんでした")

        def remember_published(media_id):
            if not database.record_published(draft_id, media_id):
                raise RuntimeError("Instagramの公開結果を保存できませんでした")

        stop_heartbeat = Event()

        def keep_alive():
            while not stop_heartbeat.wait(30):
                try:
                    database.heartbeat_publish(draft_id)
                except Exception:
                    logging.exception("Publish heartbeat failed")

        heartbeat = Thread(target=keep_alive, daemon=True)
        heartbeat.start()
        try:
            media_id, permalink = InstagramClient(config, account).publish(
                photos, draft["caption"], on_progress=report_progress,
                on_container=remember_container,
                on_publish_start=remember_publish_start,
                on_published=remember_published,
                should_cancel=check_cancelled,
            )
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=1)
        draft = database.get_draft(draft_id)
        return _finish_published(
            database, config, account, draft, photos, media_id, permalink
        )
    except PublishCancelled:
        database.event(draft_id, "publish_cancelled")
        return {"media_id": None, "permalink": "", "warning": None, "cancelled": True}
    except Exception as exc:
        logging.exception("Instagram publish failed")
        latest = database.get_draft(draft_id)
        # 公開ID保存後の追加通信エラーなら投稿済みとして復旧する。
        if latest and latest["instagram_media_id"]:
            account = accounts.get(latest["account_key"])
            photos = database.get_photos(json.loads(latest["photo_ids"]))
            return _finish_published(
                database, config, account, latest, photos,
                latest["instagram_media_id"], latest["permalink"] or "",
            )
        if latest and latest["status"] == "committing":
            # media_publish送信後の通信切断は成功の可能性がある。照合前に再試行させない。
            database.update_publish_progress(
                draft_id, progress_message="公開結果を確認中です。しばらくお待ちください"
            )
            database.event(draft_id, "publish_result_uncertain", str(exc)[:1000])
        elif database.fail_publish(draft_id, str(exc)):
            database.event(draft_id, "publish_failed", str(exc)[:1000])
        raise


def recover_stale_publishing(database, config, accounts):
    """一定時間更新がない投稿をInstagramと照合して安全に復旧する。"""
    minutes = max(5, int(getattr(config, "PUBLISH_STALE_MINUTES", 10)))
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )
    for stale in database.list_stale_publishing(cutoff):
        if not database.claim_stale_recovery(stale["id"], cutoff):
            continue
        draft = database.get_draft(stale["id"])
        photo_ids = json.loads(draft["photo_ids"])
        photos = database.get_photos(photo_ids)
        account = accounts.get(draft["account_key"])
        if not account:
            continue
        try:
            media_id = draft["instagram_media_id"]
            permalink = draft["permalink"] or ""
            if not media_id and (draft["progress"] or 0) >= 70:
                recent = InstagramClient(config, account).find_recent_post(
                    draft["caption"],
                    stale["publish_started_at"] or stale["updated_at"],
                )
                if recent:
                    media_id = recent["id"]
                    permalink = recent.get("permalink", "")
            if media_id:
                _finish_published(
                    database, config, account, draft, photos, media_id, permalink
                )
                database.event(draft["id"], "interrupted_publish_recovered")
                continue
            if draft["status"] == "committing" or (draft["progress"] or 0) >= 70:
                if database.mark_publish_unverified(draft["id"]):
                    database.event(draft["id"], "publish_result_unverified")
                continue
            message = (
                "通信切断などにより投稿処理が中断されました。"
                "公開処理の開始前だったため、再実行できます。"
            )
            if database.reset_interrupted_publish(draft["id"], message):
                database.event(draft["id"], "interrupted_publish_reset")
        except Exception as exc:
            # Instagram自体へ接続できない間は判定せず、次回に持ち越す。
            logging.exception("Interrupted publish recovery check failed")
            database.event(
                draft["id"], "interrupted_publish_check_failed", str(exc)[:1000]
            )
