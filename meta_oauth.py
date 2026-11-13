"""Facebook LoginによるInstagramプロアカウントの接続。秘密はサーバー内だけで扱う。"""
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import requests


SCOPES = ("pages_show_list", "pages_read_engagement", "instagram_basic", "instagram_content_publish")


class OAuthError(RuntimeError):
    pass


class MetaOAuth:
    def __init__(self, config):
        self.config = config
        self.http = requests.Session()
        self.base = f"https://graph.facebook.com/{config.INSTAGRAM_API_VERSION}"

    @property
    def redirect_uri(self):
        return self.config.PUBLIC_BASE_URL.rstrip("/") + "/accounts/oauth/callback"

    def configuration_error(self):
        if not self.config.META_APP_ID or not self.config.META_APP_SECRET:
            return "初期設定が必要です。.envにMETA_APP_IDとMETA_APP_SECRETを設定してください。"
        parts = urlsplit(self.config.PUBLIC_BASE_URL)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or parts.username:
            return "PUBLIC_BASE_URLに、このアプリの公開HTTPS URLを設定してください。"
        if not re.fullmatch(r"v\d+\.\d+", self.config.INSTAGRAM_API_VERSION):
            return "INSTAGRAM_API_VERSIONの形式を確認してください。"
        return ""

    def authorization_url(self, state):
        error = self.configuration_error()
        if error:
            raise OAuthError(error)
        params = dict(client_id=self.config.META_APP_ID, redirect_uri=self.redirect_uri,
                      response_type="code", state=state, auth_type="rerequest")
        if self.config.META_LOGIN_CONFIG_ID:
            params.update(config_id=self.config.META_LOGIN_CONFIG_ID,
                          override_default_response_type="true")
        else:
            params["scope"] = ",".join(SCOPES)
        return f"https://www.facebook.com/{self.config.INSTAGRAM_API_VERSION}/dialog/oauth?{urlencode(params)}"

    def _get(self, path, params=None, token=None):
        params = dict(params or {})
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            params["appsecret_proof"] = hmac.new(
                self.config.META_APP_SECRET.encode(), token.encode(), hashlib.sha256
            ).hexdigest()
        try:
            response = self.http.get(self.base + path, params=params, headers=headers,
                                     timeout=(10, 45), allow_redirects=False)
            payload = response.json()
        except (requests.RequestException, ValueError):
            raise OAuthError("Metaから応答を取得できませんでした。時間をおいて再認証してください。") from None
        if not isinstance(payload, dict):
            raise OAuthError("Metaからの応答形式を確認できませんでした。再認証してください。")
        if not response.ok or payload.get("error"):
            # Metaのmessage/URLには秘密が含まれる可能性があるため表示・記録しない。
            error = payload.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            suffix = f"（エラーコード {code}）" if isinstance(code, int) else ""
            raise OAuthError("Meta認証に失敗しました。アプリの権限・設定を確認してください。" + suffix)
        return payload

    def _list(self, path, params, token):
        after = None
        seen = set()
        while True:
            data = self._get(path, dict(params, **({"after": after} if after else {})), token)
            yield from data.get("data", [])
            paging = data.get("paging", {})
            after = paging.get("cursors", {}).get("after")
            if not paging.get("next") or not after:
                break
            if after in seen:
                raise OAuthError("Metaの一覧取得が完了しませんでした。再認証してください。")
            seen.add(after)

    def discover(self, code):
        short = self._get("/oauth/access_token", dict(
            client_id=self.config.META_APP_ID, client_secret=self.config.META_APP_SECRET,
            redirect_uri=self.redirect_uri, code=code))
        if not short.get("access_token"):
            raise OAuthError("認証トークンを取得できませんでした。")
        long = self._get("/oauth/access_token", dict(
            grant_type="fb_exchange_token", client_id=self.config.META_APP_ID,
            client_secret=self.config.META_APP_SECRET, fb_exchange_token=short["access_token"]))
        token = long.get("access_token")
        if not token:
            raise OAuthError("長期トークンを取得できませんでした。")
        debug = self._get("/debug_token", {"input_token": token},
                          self.config.META_APP_ID + "|" + self.config.META_APP_SECRET).get("data", {})
        if not debug.get("is_valid") or str(debug.get("app_id")) != self.config.META_APP_ID:
            raise OAuthError("このMetaアプリの有効なトークンとして確認できませんでした。")
        granted = {p.get("permission") for p in self._list("/me/permissions", {}, token)
                   if p.get("status") == "granted"}
        missing = set(SCOPES) - granted
        if missing:
            raise OAuthError("投稿に必要な権限が不足しています：" + ", ".join(sorted(missing)))
        expires = []
        for name in ("expires_at", "data_access_expires_at"):
            if debug.get(name):
                expires.append(datetime.fromtimestamp(int(debug[name]), timezone.utc))
        if long.get("expires_in"):
            expires.append(datetime.now(timezone.utc) + timedelta(seconds=int(long["expires_in"])))
        expires_at = min(expires).isoformat(timespec="seconds") if expires else ""
        if expires and min(expires) <= datetime.now(timezone.utc):
            raise OAuthError("認証の有効期限が切れています。再認証してください。")
        candidates = {}
        for page in self._list("/me/accounts", {"fields": "id,name,instagram_business_account", "limit": 100}, token):
            ig_id = str((page.get("instagram_business_account") or {}).get("id", ""))
            if not ig_id:
                continue
            profile = self._get("/" + ig_id, {"fields": "id,username"}, token)
            if str(profile.get("id")) != ig_id or not profile.get("username"):
                continue
            candidates[ig_id] = dict(ig_id=ig_id, username=profile["username"],
                                    page_id=str(page["id"]), page_name=page.get("name", ""),
                                    token=token, expires_at=expires_at)
        if not candidates:
            raise OAuthError("接続できるInstagramが見つかりません。Facebookページとの連携、ページの管理権限、認証時に選んだページを確認してください。")
        return list(candidates.values())
