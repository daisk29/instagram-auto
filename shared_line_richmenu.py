"""Rich-menu postback router for Crach29 / 二九写真店 shared LINE.

No direct publish from a rich menu: always show a draft-specific confirmation.
The existing, signature-verified webhook calls handle_richmenu() only after
validating the LINE user against SHARED_LINE_ALLOWED_USER_IDS.
"""
import logging
from urllib.parse import parse_qs
from shared_line_flow import (
    initialize, registered_event, enqueue_message, text_message,
    action_card, action_button, current_group, enqueue_action, proposal_messages,
)

LOG = logging.getLogger(__name__)
BRANDS = {'crach29': 'Crach29', 'photo29': '二九写真店'}
OPERATIONS = {'upload', 'preview', 'approve', 'edit', 'schedule', 'status'}


def _reply(conn, uid, content):
    enqueue_message(conn, uid, text_message(content))


def handle_richmenu(conn, event, uid):
    """Return True if this postback belongs to the rich menu; else False.

    Called from shared_line_bridge.handle_event(), not as a public endpoint.
    """
    postback = event.get('postback') or {}
    params = parse_qs(str(postback.get('data') or ''), keep_blank_values=False)
    action = (params.get('a') or [''])[0]
    if action not in ('rm_brand', 'rm_menu'):
        return False
    if not registered_event(conn, event):
        return True  # Ignore redelivery / replay.
    brand = (params.get('b') or [''])[0]
    if brand not in BRANDS:
        _reply(conn, uid, 'ブランドを確認できませんでした。メニューから選び直してください。')
        return True
    from shared_line_bridge import folder_for, brand_set, brand_get, FOLDER_ID
    if not FOLDER_ID.fullmatch(folder_for(brand)):
        LOG.error('Missing rich-menu Drive folder for %s', brand)
        _reply(conn, uid, 'このブランドの保存先が未設定です。管理者に連絡してください。')
        return True
    if brand_get(conn, uid) != brand:
        brand_set(conn, uid, brand)
    if action == 'rm_brand':
        _reply(conn, uid, '操作対象を「{}」に切り替えました。写真を送信できます。'.format(BRANDS[brand]))
        return True

    op = (params.get('op') or [''])[0]
    if op not in OPERATIONS:
        _reply(conn, uid, 'メニュー操作が認識できません。再度お試しください。')
        return True
    initialize(conn)
    if op == 'upload':
        _reply(conn, uid, '{}へ投稿する写真を送信してください。\n写真は最大10枚、文章は写真送信後3分以内に送信できます。'.format(BRANDS[brand]))
    elif op == 'status':
        rows = conn.execute('''SELECT id,stage FROM line_flow_groups
          WHERE line_user_id=? AND brand=? ORDER BY id DESC LIMIT 5''', (uid, brand)).fetchall()
        stage_names = {'collecting':'写真受信中','ready':'承認待ち','hold':'保留',
                       'scheduled':'予約済み','posted':'投稿済み','failed':'要確認',
                       'editing':'文章修正中','regenerating':'再生成中',
                       'awaiting_schedule_confirmation':'日時確認中',
                       'publish_queued':'投稿処理中', 'schedule_queued':'予約登録中',
                       'cancel_queued':'予約取消中'}
        summary = '\n'.join('#{} {}'.format(r['id'], stage_names.get(r['stage'], r['stage'])) for r in rows)
        _reply(conn, uid, '{} の最近の投稿\n{}'.format(BRANDS[brand], summary or '投稿はありません。'))
    elif op == 'approve':
        from shared_line_bridge import setting
        approvers = {v.strip() for v in setting('SHARED_LINE_APPROVER_IDS').split(',') if v.strip()}
        if uid not in approvers:
            _reply(conn, uid, '投稿承認の権限がありません。')
            return True
        rows = conn.execute('''SELECT * FROM line_flow_groups
          WHERE line_user_id=? AND brand=? AND stage='ready' AND draft_id IS NOT NULL
          ORDER BY id DESC LIMIT 3''', (uid, brand)).fetchall()
        if not rows:
            _reply(conn, uid, '承認待ちの投稿案はありません。')
            return True
        _reply(conn, uid, '{}：承認する投稿案を選んでください。'.format(BRANDS[brand]))
        for group in rows:
            enqueue_message(conn, uid, action_card('投稿案 #{}'.format(group['id']),
                '内容を確認してから最終承認してください。', [
                    action_button('内容を確認', 'preview', group['id'], group['version']),
                    action_button('投稿を承認', 'postnow', group['id'], group['version']),
                ]))
    else:
        group = current_group(conn, uid, brand)
        if not group or not group['draft_id']:
            _reply(conn, uid, '操作できる投稿案がありません。写真と文章を送信してください。')
            return True
        if op == 'preview':
            enqueue_action(conn, group, 'preview')
        elif op == 'edit':
            if group['stage'] not in ('ready', 'hold', 'failed'):
                _reply(conn, uid, 'この投稿は現在修正できません。投稿状況を確認してください。')
            else:
                _reply(conn, uid, '投稿案 #{} の修正\n\n修正: 完成した投稿文全文\n\n再生成: AIへの変更指示\n\nの形式で送信してください。'.format(group['id']))
        elif op == 'schedule':
            if group['stage'] not in ('ready', 'scheduled'):
                _reply(conn, uid, '日時指定できる承認待ちの投稿案がありません。')
            else:
                enqueue_message(conn, uid, action_card('投稿日時の指定',
                    '投稿案 #{}：日時を選択してください。確定前に再確認します。'.format(group['id']), [
                        {'type':'datetimepicker','label':'日時を選ぶ',
                         'data':'a=schedule&g={}&v={}'.format(group['id'], group['version']),
                         'mode':'datetime'},
                        action_button('内容を確認','preview',group['id'],group['version']),
                    ]))
    return True
