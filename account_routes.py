import hmac
import secrets
from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, session, url_for

from account_store import parse_settings
from meta_oauth import MetaOAuth, OAuthError


def existing_profile(profiles, candidate, target=None):
    if target:
        return profiles.get(target)
    return next((p for p in profiles.values() if p.instagram_ig_user_id == candidate["ig_id"]), None) or next(
        (p for p in profiles.values() if not p.instagram_ig_user_id
         and p.username.casefold() == candidate["username"].casefold()), None)


def register_account_routes(app, config, store, verify_csrf, login_required):
    @app.template_filter("connection_status")
    def connection_status(account):
        if account.connection_error:
            return account.connection_error
        if not account.instagram_access_token or not account.instagram_ig_user_id:
            return "未接続"
        if account.token_expires_at:
            expiry = datetime.fromisoformat(account.token_expires_at)
            days = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
            if days <= 0:
                return "期限切れ・再認証が必要です"
            if days <= 7:
                return "まもなく期限切れ・再認証してください"
        return "認証済み" if account.oauth_connected else "既存設定で接続"

    @app.get("/accounts")
    @login_required
    def accounts():
        oauth = MetaOAuth(config)
        return render_template("accounts.html", accounts=list(store.profiles().values()),
                               setup_error=oauth.configuration_error() or store.configuration_error(),
                               redirect_uri=oauth.redirect_uri)

    @app.post("/accounts/connect")
    @login_required
    def account_connect():
        verify_csrf()
        target = request.form.get("account_key", "")
        if target and target not in store.profiles():
            abort(404)
        try:
            error = store.configuration_error()
            if error:
                raise ValueError(error)
            state = secrets.token_urlsafe(32)
            url = MetaOAuth(config).authorization_url(state)
            owner = session.setdefault("oauth_owner", secrets.token_urlsafe(32))
            store.clear_flows(owner)
            store.new_flow(state, owner, "pending", {"target": target})
            session["oauth_state"] = state
            session.pop("oauth_ready", None)
            return redirect(url)
        except (ValueError, OAuthError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("accounts"))

    @app.get("/accounts/oauth/callback")
    @login_required
    def account_oauth_callback():
        expected = session.get("oauth_state", "")
        supplied = request.args.get("state", "")
        if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
            flash("認証リクエストを確認できません。管理画面から認証をやり直してください。", "error")
            return redirect(url_for("accounts"))
        session.pop("oauth_state", None)
        try:
            owner = session.get("oauth_owner", "")
            pending = store.read_flow(supplied, owner, "pending", consume=True)
            if not pending:
                raise ValueError("認証の有効時間が切れました。最初から認証してください。")
            if request.args.get("error"):
                raise ValueError("Meta認証がキャンセルされたか、許可されませんでした。")
            code = request.args.get("code", "")
            if not code:
                raise ValueError("認証コードを受信できませんでした。もう一度認証してください。")
            candidates = MetaOAuth(config).discover(code)
            if pending.get("target"):
                target = store.profiles().get(pending["target"])
                candidates = [c for c in candidates if target and (
                    c["ig_id"] == target.instagram_ig_user_id if target.instagram_ig_user_id
                    else c["username"].casefold() == target.username.casefold())]
                if not candidates:
                    raise ValueError("元のInstagramアカウントを認証できませんでした。Facebookのログイン先と選択したページを確認してください。")
            ready = secrets.token_urlsafe(32)
            store.new_flow(ready, owner, "ready", dict(target=pending.get("target"), candidates=candidates))
            session["oauth_ready"] = ready
            return redirect(url_for("account_select"))
        except (ValueError, OAuthError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("accounts"))

    @app.get("/accounts/select")
    @login_required
    def account_select():
        try:
            flow = store.read_flow(session.get("oauth_ready", ""), session.get("oauth_owner", ""), "ready")
            if not flow:
                raise ValueError("認証の有効時間が切れました。最初から認証してください。")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("accounts"))
        profiles = store.profiles()
        # トークンはテンプレートに渡さず、候補の公開項目だけを渡す。
        candidates = [dict(ig_id=c["ig_id"], username=c["username"], page_name=c["page_name"],
                           existing=existing_profile(profiles, c, flow.get("target"))) for c in flow["candidates"]]
        return render_template("account_select.html", candidates=candidates, default_prompt=config.CAPTION_PROMPT)

    @app.post("/accounts/save")
    @login_required
    def account_save():
        verify_csrf()
        try:
            ready = session.get("oauth_ready", "")
            owner = session.get("oauth_owner", "")
            flow = store.read_flow(ready, owner, "ready")
            if not flow:
                raise ValueError("認証の有効時間が切れました。最初から認証してください。")
            ig_id = request.form.get("ig_id", "")
            candidate = next((c for c in flow["candidates"] if c["ig_id"] == ig_id), None)
            if not candidate:
                raise ValueError("認証した一覧からアカウントを選択してください。")
            existing = existing_profile(store.profiles(), candidate, flow.get("target"))
            settings = {} if existing else parse_settings(request.form, config.CAPTION_PROMPT)
            key = store.save_connection(ready, owner, ig_id, settings)
            session.pop("oauth_ready", None)
            flash("Instagramアカウントを接続しました。", "success")
            return redirect(url_for("account_settings", account_key=key))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("account_select"))

    @app.post("/accounts/cancel")
    @login_required
    def account_cancel():
        verify_csrf()
        store.clear_flows(session.get("oauth_owner", ""))
        session.pop("oauth_ready", None)
        session.pop("oauth_state", None)
        return redirect(url_for("accounts"))

    @app.route("/accounts/<account_key>/settings", methods=["GET", "POST"])
    @login_required
    def account_settings(account_key):
        account = store.profiles().get(account_key)
        if not account:
            abort(404)
        if request.method == "POST":
            verify_csrf()
            try:
                settings = parse_settings(request.form, config.CAPTION_PROMPT)
                store.update_settings(account_key, settings)
                flash("アカウント設定を保存しました。", "success")
                return redirect(url_for("account_settings", account_key=account_key))
            except ValueError as exc:
                flash(str(exc), "error")
        return render_template("account_settings.html", account=account)
