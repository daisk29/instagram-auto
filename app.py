import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request, send_file,
    session, url_for,
)

from caption_client import CaptionClient
from account_store import AccountStore, AccountRegistry
from account_routes import register_account_routes
from config import Config
from database import Database
from drive_client import DriveClient
from direct_upload import register_direct_upload_routes
from line_drive_bridge import line_drive_blueprint
from shared_line_bridge import shared_line_blueprint
from publish_service import publish_claimed_draft, recover_stale_publishing


app = Flask(__name__)
app.register_blueprint(line_drive_blueprint)
app.register_blueprint(shared_line_blueprint)
app.config.from_object(Config)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=Config.PUBLIC_BASE_URL.startswith("https://"),
)

Path(app.config["DATABASE_PATH"]).parent.mkdir(parents=True, exist_ok=True)
Path(app.config["MEDIA_DIR"]).mkdir(parents=True, exist_ok=True)
Path(app.config["LOG_FILE"]).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=app.config["LOG_FILE"], level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
db = Database(app.config["DATABASE_PATH"])
db.init()
account_store = AccountStore(Config, db)
ACCOUNT_PROFILES = AccountRegistry(account_store)
DEFAULT_ACCOUNT_KEY = "29shashinten"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def verify_csrf():
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        abort(400, "不正なリクエストです")


app.jinja_env.globals["csrf_token"] = csrf_token


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.path.startswith("/accounts"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def draft_with_photos(draft):
    ids = json.loads(draft["photo_ids"])
    return {"draft": draft, "photos": db.get_photos(ids)}


def account_profile(account_key=None):
    profiles = ACCOUNT_PROFILES
    if account_key:
        key = account_key
    elif DEFAULT_ACCOUNT_KEY in profiles:
        key = DEFAULT_ACCOUNT_KEY
    else:
        key = next(iter(profiles), None)
    if not key:
        abort(400, "Instagramアカウントが未登録です")
    profile = ACCOUNT_PROFILES.get(key)
    if not profile:
        abort(400, "不明なInstagramアカウントです")
    return profile


def draft_account(draft):
    return account_profile(draft["account_key"])


def parse_schedule(value):
    if not value:
        raise ValueError("予約日時を選択してください")
    try:
        local = datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(
            tzinfo=ZoneInfo(Config.APP_TIMEZONE)
        )
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("予約日時を正しく選択してください") from exc
    scheduled = local.astimezone(timezone.utc)
    if scheduled <= datetime.now(timezone.utc) + timedelta(seconds=30):
        raise ValueError("現在より後の日時を選択してください")
    return scheduled.isoformat(timespec="seconds")


def default_schedule_value():
    zone = ZoneInfo(Config.APP_TIMEZONE)
    now = datetime.now(zone)
    target = now.replace(
        hour=Config.DEFAULT_SCHEDULE_HOUR, minute=0, second=0, microsecond=0
    )
    if target <= now + timedelta(minutes=5):
        target += timedelta(days=1)
    return target.strftime("%Y-%m-%dT%H:%M")


@app.template_filter("schedule_local")
def schedule_local(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone(
            ZoneInfo(Config.APP_TIMEZONE)
        ).strftime("%Y年%m月%d日 %H:%M")
    except (TypeError, ValueError):
        return value


@app.get("/health")
def health():
    return {"status": "ok"}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        verify_csrf()
        good_user = hmac.compare_digest(
            request.form.get("username", ""), app.config["ADMIN_USERNAME"]
        )
        good_pass = hmac.compare_digest(
            request.form.get("password", ""), app.config["ADMIN_PASSWORD"]
        )
        if good_user and good_pass and app.config["ADMIN_PASSWORD"]:
            session.clear()
            session["logged_in"] = True
            return redirect(url_for("index"))
        flash("ユーザー名またはパスワードが違います", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    verify_csrf()
    account_store.clear_flows(session.get("oauth_owner", ""))
    session.clear()
    return redirect(url_for("login"))


register_account_routes(app, Config, account_store, verify_csrf, login_required)
register_direct_upload_routes(app, Config, db, account_profile, login_required, verify_csrf)


@app.get("/")
@login_required
def index():
    profiles = list(ACCOUNT_PROFILES.values())
    if not profiles:
        return redirect(url_for("accounts"))
    recover_stale_publishing(db, Config, ACCOUNT_PROFILES)
    selected_account = account_profile(request.args.get("account"))
    key = selected_account.key
    inbox = db.list_photos("inbox", key)
    draft_rows = db.list_drafts(key)
    drafts = [draft_with_photos(row) for row in draft_rows]
    counts = {
        "inbox": len(inbox),
        "review": len(db.list_photos("review", key)),
        "hold": len(db.list_photos("hold", key)),
        "scheduled": len(db.list_photos("scheduled", key)),
        "posted": len(db.list_photos("posted", key)),
    }
    return render_template(
        "index.html", inbox=inbox, drafts=drafts, counts=counts,
        accounts=profiles, selected_account=selected_account,
    )


@app.post("/sync")
@login_required
def sync_drive():
    verify_csrf()
    account = account_profile(request.form.get("account_key"))
    try:
        imported, skipped = DriveClient(Config, account).sync(db)
        flash(
            f"@{account.username}のDriveを読み込みました："
            f"新規{imported}件、対象外・登録済み{skipped}件",
            "success",
        )
    except Exception as exc:
        logging.exception("Drive sync failed")
        flash(str(exc), "error")
    return redirect(url_for("index", account=account.key))


@app.post("/drafts")
@login_required
def create_draft():
    verify_csrf()
    account = account_profile(request.form.get("account_key"))
    try:
        photo_ids = [int(value) for value in request.form.getlist("photo_ids")]
        if not 1 <= len(photo_ids) <= 10 or len(set(photo_ids)) != len(photo_ids):
            raise ValueError("写真を1〜10枚、重複せずに選択してください")
        photos = db.get_photos(photo_ids)
        if len(photos) != len(photo_ids) or any(
            p["status"] != "inbox" or p["account_key"] != account.key
            for p in photos
        ):
            raise ValueError("選択した写真の状態を確認してください")
        caption = CaptionClient(Config, account).generate(
            photos, request.form.get("instruction", "")
        )
        draft_id = db.create_draft(photo_ids, caption, account.key)
        return redirect(url_for("edit_draft", draft_id=draft_id))
    except Exception as exc:
        logging.exception("Draft creation failed")
        flash(str(exc), "error")
        return redirect(url_for("index", account=account.key))


@app.get("/drafts/<int:draft_id>")
@login_required
def edit_draft(draft_id):
    recover_stale_publishing(db, Config, ACCOUNT_PROFILES)
    draft = db.get_draft(draft_id)
    if not draft:
        abort(404)
    return render_template(
        "edit.html", account=draft_account(draft),
        default_schedule=default_schedule_value(), **draft_with_photos(draft)
    )


@app.get("/drafts/<int:draft_id>/status")
@login_required
def draft_status(draft_id):
    recover_stale_publishing(db, Config, ACCOUNT_PROFILES)
    draft = db.get_draft(draft_id)
    if not draft:
        abort(404)
    progress = draft["progress"] or 0
    status = draft["status"]
    return jsonify(
        {
            "status": status,
            "progress": progress,
            "message": draft["progress_message"] or "待機中",
            "complete": status in ("error", "cancelled", "unverified")
            or (status == "posted" and progress >= 100),
            "cancellable": status == "publishing"
            and not draft["instagram_media_id"],
        }
    )


@app.post("/drafts/<int:draft_id>/cancel-publish")
@login_required
def cancel_publish(draft_id):
    verify_csrf()
    draft = db.get_draft(draft_id)
    if not draft:
        abort(404)
    if db.cancel_publish(draft_id):
        db.event(draft_id, "publish_cancel_requested")
        return jsonify(
            {"ok": True, "status": "cancelled", "message": "投稿処理を中止しました"}
        )
    current = db.get_draft(draft_id)
    return jsonify(
        {
            "ok": False,
            "status": current["status"],
            "message": "Instagramへの公開処理が始まったため中止できません",
        }
    ), 409


@app.post("/drafts/<int:draft_id>/confirm-unpublished")
@login_required
def confirm_unpublished(draft_id):
    verify_csrf()
    if not db.get_draft(draft_id):
        abort(404)
    if request.form.get("confirm") != "yes":
        flash("Instagramで未投稿と確認してからチェックしてください", "error")
    elif db.confirm_unpublished(draft_id):
        db.event(draft_id, "unpublished_confirmed")
        flash("未投稿として確認しました。内容を確認して再試行できます", "success")
    else:
        flash("投稿状態が変わったため更新できませんでした", "error")
    return redirect(url_for("edit_draft", draft_id=draft_id))


@app.post("/drafts/<int:draft_id>/delete")
@login_required
def delete_draft(draft_id):
    verify_csrf()
    draft = db.get_draft(draft_id)
    if not draft:
        abort(404)
    account_key = draft["account_key"]
    if db.delete_inactive_draft(draft_id):
        flash("投稿案を削除し、写真を未処理へ戻しました", "success")
        return redirect(url_for("index", account=account_key))
    flash("保留または中止された投稿案だけ削除できます", "error")
    return redirect(url_for("edit_draft", draft_id=draft_id))


@app.post("/drafts/<int:draft_id>/save")
@login_required
def save_draft(draft_id):
    verify_csrf()
    if not db.get_draft(draft_id):
        abort(404)
    if db.update_editable_draft(draft_id, request.form.get("caption", "").strip()):
        db.event(draft_id, "saved")
        flash("キャプションを保存しました", "success")
    else:
        flash("投稿処理中または投稿済みのため保存できません", "error")
    return redirect(url_for("edit_draft", draft_id=draft_id))


@app.post("/drafts/<int:draft_id>/regenerate")
@login_required
def regenerate_draft(draft_id):
    verify_csrf()
    draft = db.get_draft(draft_id)
    if not draft:
        abort(404)
    if draft["status"] not in ("draft", "hold", "error", "cancelled"):
        flash("投稿処理中または投稿済みのため再生成できません", "error")
        return redirect(url_for("edit_draft", draft_id=draft_id))
    try:
        account = draft_account(draft)
        photos = db.get_photos(json.loads(draft["photo_ids"]))
        caption = CaptionClient(Config, account).generate(
            photos, request.form.get("instruction", "")
        )
        if not db.update_editable_draft(draft_id, caption, "draft"):
            raise ValueError("投稿状態が変わったため再生成結果を保存できませんでした")
        db.event(draft_id, "regenerated", request.form.get("instruction", "")[:500])
        flash("キャプションを再生成しました", "success")
    except Exception as exc:
        logging.exception("Caption regeneration failed")
        flash(str(exc), "error")
    return redirect(url_for("edit_draft", draft_id=draft_id))


@app.post("/drafts/<int:draft_id>/hold")
@login_required
def hold_draft(draft_id):
    verify_csrf()
    draft = db.get_draft(draft_id)
    if not draft:
        abort(404)
    if db.update_editable_draft(
        draft_id, request.form.get("caption", draft["caption"]), "hold"
    ):
        db.event(draft_id, "held")
        flash("保留にしました", "success")
        return redirect(url_for("index", account=draft["account_key"]))
    flash("投稿処理中または投稿済みのため保留にできません", "error")
    return redirect(url_for("edit_draft", draft_id=draft_id))


@app.post("/drafts/<int:draft_id>/schedule")
@login_required
def schedule_draft(draft_id):
    verify_csrf()
    draft = db.get_draft(draft_id)
    if not draft:
        abort(404)
    caption = request.form.get("caption", "").strip()
    if not caption or request.form.get("confirm") != "yes":
        flash("内容を確認し、確認欄にチェックしてください", "error")
        return redirect(url_for("edit_draft", draft_id=draft_id))
    try:
        scheduled_at = parse_schedule(request.form.get("scheduled_at_local", ""))
        if not db.schedule_draft(draft_id, caption, scheduled_at):
            raise ValueError("予約済み、投稿済み、または投稿処理中のため予約できません")
        db.event(draft_id, "scheduled", scheduled_at)
        flash(f"{schedule_local(scheduled_at)}に予約しました", "success")
    except Exception as exc:
        logging.exception("Schedule creation failed")
        flash(str(exc), "error")
    return redirect(url_for("edit_draft", draft_id=draft_id))


@app.post("/drafts/<int:draft_id>/cancel-schedule")
@login_required
def cancel_schedule(draft_id):
    verify_csrf()
    draft = db.get_draft(draft_id)
    if not draft:
        abort(404)
    if db.cancel_schedule(draft_id):
        db.event(draft_id, "schedule_cancelled")
        flash("予約を取り消しました", "success")
    else:
        flash("予約投稿はすでに開始または完了しています", "error")
    return redirect(url_for("edit_draft", draft_id=draft_id))


@app.post("/drafts/<int:draft_id>/publish")
@login_required
def publish_draft(draft_id):
    verify_csrf()
    draft = db.get_draft(draft_id)
    if not draft:
        abort(404)
    if draft["status"] in ("posted", "publishing", "committing", "unverified", "scheduled"):
        flash("この投稿は投稿済み、または現在投稿処理中です", "error")
        return redirect(url_for("edit_draft", draft_id=draft_id))
    caption = request.form.get("caption", "").strip()
    if not caption or request.form.get("confirm") != "yes":
        flash("内容を確認し、確認欄にチェックしてください", "error")
        return redirect(url_for("edit_draft", draft_id=draft_id))
    if not db.claim_publish(draft_id, caption):
        flash("別の投稿処理が開始済みです", "error")
        return redirect(url_for("edit_draft", draft_id=draft_id))
    try:
        result = publish_claimed_draft(db, Config, ACCOUNT_PROFILES, draft_id)
        if result.get("cancelled"):
            flash("投稿処理を中止しました", "success")
            return redirect(url_for("edit_draft", draft_id=draft_id))
        if result["warning"]:
            flash(result["warning"], "error")
        else:
            flash("Instagramへ投稿しました", "success")
        return redirect(url_for("edit_draft", draft_id=draft_id))
    except Exception as exc:
        flash(f"投稿に失敗しました：{exc}", "error")
        return redirect(url_for("edit_draft", draft_id=draft_id))


@app.get("/media/<token>")
def media(token):
    photo = db.photo_by_token(token)
    if not photo:
        abort(404)
    path = Path(photo["local_path"])
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype=photo["mime_type"], max_age=300)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
