"""cronから呼び出し、期限を迎えた予約投稿を処理する。"""

import logging
from pathlib import Path

from config import Config
from account_store import AccountStore, AccountRegistry
from database import Database, utcnow
from publish_service import publish_claimed_draft, recover_stale_publishing


def main():
    Path(Config.LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=Config.LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    database = Database(Config.DATABASE_PATH)
    database.init()
    accounts = AccountRegistry(AccountStore(Config, database))
    recover_stale_publishing(database, Config, accounts)
    due_at = utcnow()
    due_drafts = database.list_due_drafts(due_at)

    if not due_drafts:
        return

    for draft in due_drafts:
        if not database.claim_scheduled_publish(draft["id"], due_at):
            continue
        try:
            result = publish_claimed_draft(
                database, Config, accounts, draft["id"]
            )
            logging.info(
                "Scheduled draft %s result: %s",
                draft["id"], result.get("media_id") or "cancelled",
            )
        except Exception:
            # 詳細はpublish_claimed_draftがDBとログへ保存する。
            logging.exception("Scheduled draft %s failed", draft["id"])


if __name__ == "__main__":
    main()
