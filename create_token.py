"""Macで一度だけ実行し、Google Drive用token.jsonを作成する。"""
from config import Config
from drive_client import DriveClient


if __name__ == "__main__":
    DriveClient(Config).credentials(interactive=True)
    print(f"認証完了: {Config.GOOGLE_TOKEN_FILE}")
