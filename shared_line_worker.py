"""Dedicated shared LINE worker; existing BISYU worker and queue are untouched."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from dotenv import load_dotenv
load_dotenv(ROOT / '.env', override=False)

if not os.getenv('SHARED_LINE_CHANNEL_ACCESS_TOKEN'):
    raise SystemExit('Missing SHARED_LINE_CHANNEL_ACCESS_TOKEN')

# The preexisting worker reads generic keys; map them inside this child process only.
os.environ['LINE_CHANNEL_ACCESS_TOKEN'] = os.environ['SHARED_LINE_CHANNEL_ACCESS_TOKEN']
os.environ['LINE_DRIVE_DATA_DIR'] = str(ROOT / '.shared-line-data')

from line_drive_common import db
from line_drive_worker import main, push_user_message


def flush_notices():
    # Pending notices are kept until successful push. No duplicates when two workers
    # run simultaneously because a single lock is taken in the existing worker.
    # Notice delivery is intentionally separate and best-effort: a failed push is retried.
    import fcntl
    lock_path = ROOT / '.shared-line-data' / 'notice.lock'
    with lock_path.open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        conn = db()
        try:
            for item in conn.execute('SELECT id,line_user_id,body FROM notices ORDER BY id LIMIT 30').fetchall():
                try:
                    push_user_message(item['line_user_id'], item['body'])
                    conn.execute('DELETE FROM notices WHERE id=?', (item['id'],))
                    conn.commit()
                except Exception:
                    import logging
                    logging.exception('Failed to send shared LINE notice')
                    break
        finally:
            conn.close()


if __name__ == '__main__':
    from shared_line_bridge import database
    # Ensure notice table exists even if worker runs before the first webhook.
    database().close()
    code = main()
    if len(sys.argv) == 1:
        flush_notices()
        # Photo delivery may finish AFTER the webhook-side flow check.
        # Re-run the flow worker as soon as Drive upload has completed.
        import subprocess
        import os
        py = ROOT / 'venv' / 'bin' / 'python'
        log_fd = os.open(str(ROOT / '.shared-line-data' / 'flow.log'),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            subprocess.Popen([str(py), str(ROOT / 'shared_line_flow_worker.py')],
                             stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                             cwd=str(ROOT), close_fds=True, start_new_session=True)
        finally:
            os.close(log_fd)
    sys.exit(code)
