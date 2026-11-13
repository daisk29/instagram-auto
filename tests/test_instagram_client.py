import sys
import types
import unittest
from unittest.mock import Mock, patch

# requestsがない最小テスト環境だけ代用品を使う。
try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.Session = Mock(return_value=Mock())
    sys.modules["requests"] = requests_stub

from instagram_client import InstagramClient


class Config:
    INSTAGRAM_API_VERSION = "v26.0"
    INSTAGRAM_IG_USER_ID = "ig-user"
    INSTAGRAM_ACCESS_TOKEN = "token"
    PUBLIC_BASE_URL = "https://example.com"


class InstagramClientTest(unittest.TestCase):
    def test_bearer_token_is_not_in_query_or_form(self):
        client = InstagramClient(Config)
        response = Mock(ok=True, json=Mock(return_value={"id": "ok"}))
        client.http.get = Mock(return_value=response)
        client.http.post = Mock(return_value=response)
        client._get("/id", {"fields": "id"})
        client._post("/id/media", {"caption": "caption"})
        self.assertNotIn("access_token", client.http.get.call_args.kwargs["params"])
        self.assertNotIn("access_token", client.http.post.call_args.kwargs["data"])
        self.assertEqual(client.http.get.call_args.kwargs["headers"]["Authorization"], "Bearer token")

    def test_error_text_hides_access_token(self):
        client = InstagramClient(Config)
        response = Mock(ok=False, json=Mock(return_value={"error": {"message": "invalid token"}}))
        with self.assertRaises(RuntimeError) as error:
            client._json(response)
        self.assertNotIn("token", str(error.exception))

    def test_media_status_is_checked_every_second(self):
        client = InstagramClient(Config)
        client._get = Mock(
            side_effect=[
                {"status_code": "IN_PROGRESS"},
                {"status_code": "FINISHED"},
            ]
        )

        with patch("instagram_client.time.sleep") as sleep:
            client._wait_until_ready("container")

        sleep.assert_called_once_with(1)
        self.assertEqual(client._get.call_count, 2)

    def test_published_id_is_reported_before_detail_lookup(self):
        client = InstagramClient(Config)
        client._create_single = Mock(return_value="container")
        client._wait_until_ready = Mock()
        client._post = Mock(return_value={"id": "media-123"})
        client._get = Mock(side_effect=RuntimeError("connection lost"))
        published = Mock()

        media_id, permalink = client.publish(
            [{"public_token": "photo"}], "caption", on_published=published
        )

        published.assert_called_once_with("media-123")
        self.assertEqual(media_id, "media-123")
        self.assertEqual(permalink, "")

    def test_cancel_after_image_upload_prevents_publish(self):
        client = InstagramClient(Config)
        client._create_single = Mock(return_value="container")
        client._post = Mock()
        checks = {"count": 0}

        def cancel_after_upload():
            checks["count"] += 1
            if checks["count"] >= 2:
                raise RuntimeError("cancelled")

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            client.publish(
                [{"public_token": "photo"}], "caption",
                should_cancel=cancel_after_upload,
            )

        client._post.assert_not_called()

    def test_publish_start_runs_before_media_publish(self):
        client = InstagramClient(Config)
        client._create_single = Mock(return_value="container")
        client._wait_until_ready = Mock()
        calls = []

        def publish_start():
            calls.append("commit")

        def post(path, data):
            calls.append(path)
            return {"id": "media-123"}

        client._post = Mock(side_effect=post)
        client._get = Mock(return_value={"id": "media-123", "permalink": "url"})
        client.publish(
            [{"public_token": "photo"}], "caption",
            on_publish_start=publish_start,
        )

        self.assertEqual(calls[0], "commit")
        self.assertEqual(calls[1], "/ig-user/media_publish")


if __name__ == "__main__":
    unittest.main()
