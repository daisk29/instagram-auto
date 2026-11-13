import logging
import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

import requests


class InstagramClient:
    def __init__(self, config, account=None):
        self.version = config.INSTAGRAM_API_VERSION
        self.user_id = (
            account.instagram_ig_user_id if account else config.INSTAGRAM_IG_USER_ID
        )
        self.token = (
            account.instagram_access_token if account else config.INSTAGRAM_ACCESS_TOKEN
        )
        self.public_base_url = config.PUBLIC_BASE_URL
        secret = getattr(config, "META_APP_SECRET", "")
        self.proof = (hmac.new(secret.encode(), self.token.encode(), hashlib.sha256).hexdigest()
                      if secret and account and getattr(account, "oauth_connected", False) else "")
        # 同じ投稿内のAPI呼び出しでHTTPS接続を再利用する。
        self.http = requests.Session()
        if account and getattr(account, "connection_error", ""):
            raise RuntimeError(account.connection_error)
        expires = getattr(account, "token_expires_at", "") if account else ""
        if expires and datetime.fromisoformat(expires) <= datetime.now(timezone.utc):
            raise RuntimeError(f"@{account.username}の認証期限が切れています。アカウント管理から再認証してください。")
        if not all((self.user_id, self.token, self.public_base_url)):
            account_name = f"@{account.username}" if account else "Instagram"
            raise RuntimeError(
                f"{account_name}のInstagram API設定またはPUBLIC_BASE_URLが不足しています"
            )

    @property
    def base(self):
        return f"https://graph.facebook.com/{self.version}"

    def publish(
        self, photos, caption, on_progress=None,
        on_container=None, on_publish_start=None, on_published=None,
        should_cancel=None,
    ):
        progress = on_progress or (lambda percent, message: None)
        check_cancel = should_cancel or (lambda: None)
        check_cancel()
        progress(15, "Instagramへ画像を送信しています")
        if len(photos) == 1:
            creation_id = self._create_single(photos[0], caption)
        elif 2 <= len(photos) <= 10:
            children = []
            for photo in photos:
                check_cancel()
                children.append(self._create_child(photo))
                check_cancel()
            progress(35, "カルーセルを準備しています")
            creation_id = self._post(
                f"/{self.user_id}/media",
                {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption},
            )["id"]
        else:
            raise ValueError("Instagram投稿は1〜10枚にしてください")
        check_cancel()
        if on_container:
            on_container(creation_id)
        progress(45, "Instagram側で画像を処理しています")
        self._wait_until_ready(creation_id, should_cancel=check_cancel)
        check_cancel()
        if on_publish_start:
            on_publish_start()
        progress(70, "Instagramで投稿を公開しています")
        media_id = self._post(
            f"/{self.user_id}/media_publish", {"creation_id": creation_id}
        )["id"]
        # 公開成功を最優先で永続化する。以降の通信が切れても再投稿させない。
        if on_published:
            on_published(media_id)
        progress(82, "公開した投稿を確認しています")
        try:
            detail = self._get(f"/{media_id}", {"fields": "id,permalink"})
        except Exception:
            logging.exception("Published media detail lookup failed")
            detail = {}
        return media_id, detail.get("permalink", "")

    def find_recent_post(self, caption, started_at):
        """公開要求直後に通信が切れた場合、最近の投稿から結果を照合する。"""
        response = self._get(
            f"/{self.user_id}/media",
            {"fields": "id,caption,permalink,timestamp", "limit": 25},
        )
        try:
            since = datetime.fromisoformat(started_at)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            since = datetime.now(timezone.utc) - timedelta(hours=1)
        since -= timedelta(minutes=2)

        for item in response.get("data", []):
            if item.get("caption", "") != caption:
                continue
            timestamp = item.get("timestamp")
            if not timestamp:
                continue
            try:
                posted_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if posted_at >= since:
                return item
        return None

    def _image_url(self, photo):
        return f"{self.public_base_url}/media/{photo['public_token']}"

    def _create_single(self, photo, caption):
        return self._post(
            f"/{self.user_id}/media",
            {"image_url": self._image_url(photo), "caption": caption},
        )["id"]

    def _create_child(self, photo):
        return self._post(
            f"/{self.user_id}/media",
            {"image_url": self._image_url(photo), "is_carousel_item": "true"},
        )["id"]

    def _wait_until_ready(self, container_id, should_cancel=None):
        check_cancel = should_cancel or (lambda: None)
        for _ in range(60):
            check_cancel()
            status = self._get(f"/{container_id}", {"fields": "status_code"}).get("status_code")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise RuntimeError("Instagram側でメディア処理に失敗しました")
            # 完了を1秒間隔で検出し、従来の5秒間隔より待ち時間を短縮する。
            time.sleep(1)
        raise TimeoutError("Instagram側のメディア処理が時間内に完了しませんでした")

    def _post(self, path, data):
        data = dict(data)
        if self.proof:
            data["appsecret_proof"] = self.proof
        try:
            response = self.http.post(self.base + path, data=data,
                                      headers={"Authorization": f"Bearer {self.token}"}, timeout=60)
        except requests.RequestException:
            raise RuntimeError("Instagramと通信できませんでした。通信状況を確認してください。") from None
        return self._json(response)

    def _get(self, path, params):
        params = dict(params)
        if self.proof:
            params["appsecret_proof"] = self.proof
        try:
            response = self.http.get(self.base + path, params=params,
                                     headers={"Authorization": f"Bearer {self.token}"}, timeout=30)
        except requests.RequestException:
            raise RuntimeError("Instagramと通信できませんでした。通信状況を確認してください。") from None
        return self._json(response)

    def _json(self, response):
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Instagram APIが不正な応答を返しました: HTTP {response.status_code}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Instagram APIからの応答形式を確認できませんでした")
        if not response.ok or "error" in payload:
            error = payload.get("error") or {}
            message = str(error.get("message", f"HTTP {response.status_code}")) if isinstance(error, dict) else f"HTTP {response.status_code}"
            message = message.replace(self.token, "[非表示]")
            raise RuntimeError(f"Instagram APIエラー: {message}")
        return payload
