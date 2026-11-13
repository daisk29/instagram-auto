"""美趣用のInstagram IDと長期アクセストークンを.envへ登録する。"""

import getpass
import os
from pathlib import Path

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)
API_VERSION = os.getenv("INSTAGRAM_API_VERSION", "v26.0")
TARGET_USERNAME = "bisyu_1995"


def graph_get(path, params):
    response = requests.get(
        f"https://graph.facebook.com/{API_VERSION}/{path.lstrip('/')}",
        params=params,
        timeout=45,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Meta APIが不正な応答を返しました: HTTP {response.status_code}"
        ) from exc
    if not response.ok or "error" in payload:
        message = payload.get("error", {}).get("message", str(payload))
        raise RuntimeError(message)
    return payload


def update_env(values):
    lines = (
        ENV_PATH.read_text(encoding="utf-8").splitlines()
        if ENV_PATH.exists()
        else []
    )
    remaining = dict(values)
    output = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    if remaining:
        if output and output[-1]:
            output.append("")
        output.append("# Bisyu account")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    ENV_PATH.write_text("\n".join(output) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)


def main():
    print("美趣 Instagram設定")
    print(
        "Graph API ExplorerではPost Managerアプリを選び、美趣に紐づく"
        "Facebookページを許可してください。"
    )
    app_id = input("MetaアプリID: ").strip()
    app_secret = getpass.getpass(
        "Meta App Secret（画面には表示されません）: "
    ).strip()
    short_token = getpass.getpass(
        "Graph API Explorerの短期トークン（画面には表示されません）: "
    ).strip()
    if not all((app_id, app_secret, short_token)):
        raise SystemExit("入力が不足しています")

    try:
        exchanged = graph_get(
            "oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            },
        )
        long_token = exchanged["access_token"]
        pages = graph_get(
            "me/accounts",
            {
                "fields": (
                    "id,name,access_token,"
                    "instagram_business_account{id,username}"
                ),
                "access_token": long_token,
                "limit": 100,
            },
        ).get("data", [])
    except (KeyError, RuntimeError) as exc:
        raise SystemExit(f"Meta設定に失敗しました: {exc}") from exc

    selected = None
    instagram = None
    discovered = []
    for page in pages:
        ig = page.get("instagram_business_account") or {}
        username = ig.get("username", "")
        if username:
            discovered.append(f"{page.get('name', '名称不明')}: @{username}")
        if username.lower() == TARGET_USERNAME.lower():
            selected = page
            instagram = ig
            break

    if not selected or not instagram:
        visible = " / ".join(discovered) if discovered else "なし"
        raise SystemExit(
            f"@{TARGET_USERNAME}が見つかりません。取得結果: {visible}\n"
            "Graph API Explorerで美趣に紐づくFacebookページを許可してから、"
            "新しいトークンを生成してください。"
        )

    update_env(
        {
            "BISYU_ACCOUNT_LABEL": "美趣",
            "BISYU_INSTAGRAM_USERNAME": TARGET_USERNAME,
            "BISYU_INSTAGRAM_IG_USER_ID": instagram["id"],
            "BISYU_INSTAGRAM_ACCESS_TOKEN": selected["access_token"],
        }
    )
    print("\n美趣の設定を保存しました")
    print("Facebookページ:", selected.get("name", "不明"))
    print("Instagram: @" + instagram.get("username", "不明"))
    print("秘密情報は.envだけに保存し、App Secretは保存していません。")


if __name__ == "__main__":
    main()
