"""Provision shared LINE rich menus with official Messaging API.

Does not touch BISYU's LINE channel or Instagram's posting settings.
The install command creates/updates menus & aliases but does not assign them.
The activate command links the Crach29 menu only to allowed staff (not all friends).
"""
import argparse
import json
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
APP_ROOT = ROOT
load_dotenv(APP_ROOT / '.env', override=False)
API = 'https://api.line.me/v2/bot'
DATA_API = 'https://api-data.line.me/v2/bot'
TOKEN = os.getenv('SHARED_LINE_CHANNEL_ACCESS_TOKEN', '').strip()
STATE = APP_ROOT / '.shared-line-data' / 'richmenu-state.json'
BRANDS = ('crach29', 'photo29')
ALIAS = {'crach29':'crach29-main','photo29':'photo29-main'}
FILE = {'crach29':'richmenu_crach29.png','photo29':'richmenu_photo29.png'}
W, H, TAB = 2500, 1686, 366
OPS = ('upload','preview','approve','edit','schedule','status')


def request(method, url, **kwargs):
    headers={'Authorization':'Bearer '+TOKEN}
    headers.update(kwargs.pop('headers',{}))
    response=requests.request(method,url,headers=headers,timeout=20,**kwargs)
    # Do not print request bodies or authorization headers, even on errors.
    if response.status_code >= 400:
        reason=response.text[:220].replace('\n',' ')
        raise RuntimeError('LINE API returned HTTP {}: {}'.format(response.status_code,reason))
    if response.content:
        try:
            return response.json()
        except ValueError:
            return {}
    return {}


def action_switch(brand):
    return {'type':'richmenuswitch','richMenuAliasId':ALIAS[brand],
            'data':'a=rm_brand&b='+brand}


def menu_object(brand):
    areas=[]
    for index,b in enumerate(BRANDS):
        areas.append({'bounds':{'x':index*1250,'y':0,'width':1250,'height':TAB},
                      'action':action_switch(b)})
    for i,op in enumerate(OPS):
        c=i%3; r=i//3
        x0=W*c//3; x1=W*(c+1)//3
        y0=TAB+(H-TAB)*r//2; y1=TAB+(H-TAB)*(r+1)//2
        areas.append({'bounds':{'x':x0,'y':y0,'width':x1-x0,'height':y1-y0},
                      'action':{'type':'postback','data':'a=rm_menu&b={}&op={}'.format(brand,op)}})
    return {'size':{'width':W,'height':H},'selected':True,
            'name':'Crach29 / 29 shared '+brand,'chatBarText':'投稿メニュー', 'areas':areas}


def user_ids():
    value=os.getenv('SHARED_LINE_ALLOWED_USER_IDS','')
    users=[s.strip() for s in value.split(',') if s.strip()]
    if not users or any(not re.fullmatch(r'U[0-9a-fA-F]{32}',s) for s in users):
        raise RuntimeError('SHARED_LINE_ALLOWED_USER_IDS is missing/invalid')
    return list(dict.fromkeys(users))


def precheck():
    from PIL import Image
    if not TOKEN:
        raise RuntimeError('SHARED_LINE_CHANNEL_ACCESS_TOKEN is missing')
    for brand in BRANDS:
        path=ROOT/FILE[brand]
        if not path.is_file():
            raise RuntimeError('Image missing: '+str(path))
        with Image.open(path) as im:
            if im.size!=(W,H) or im.format!='PNG' or path.stat().st_size>1024*1024:
                raise RuntimeError('Invalid LINE menu image: '+str(path))
    # Image can be validated even if account IDs have not yet been provided.
    users=user_ids()
    inbox_c=os.getenv('CRACH29_DRIVE_INBOX_FOLDER_ID','').strip()
    inbox_p=os.getenv('GOOGLE_INBOX_FOLDER_ID','').strip()
    if not inbox_c or not inbox_p or inbox_c==inbox_p:
        raise RuntimeError('Crach29 and 二九写真店 need separate Drive inbox folder IDs')
    approvers={v.strip() for v in os.getenv('SHARED_LINE_APPROVER_IDS','').split(',') if v.strip()}
    if not approvers or not approvers.intersection(users):
        raise RuntimeError('No authorized LINE approver is registered')
    return users


def load_state():
    if not STATE.exists(): return {}
    return json.loads(STATE.read_text(encoding='utf-8'))


def save_state(state):
    STATE.parent.mkdir(mode=0o700,exist_ok=True)
    os.chmod(STATE.parent,0o700)
    temp=STATE.with_suffix('.new')
    temp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.chmod(0o600)
    os.replace(temp,STATE)


def ensure_menu(brand,state):
    entry=state.setdefault(brand,{})
    rid=entry.get('richMenuId')
    if not rid:
        # Validate the official schema before changing any channel settings.
        request('POST',API+'/richmenu/validate',json=menu_object(brand))
        response=request('POST',API+'/richmenu',json=menu_object(brand))
        rid=response.get('richMenuId')
        if not rid:
            raise RuntimeError('LINE did not return richMenuId')
        entry.update({'richMenuId':rid,'uploaded':False})
        save_state(state)
    if not entry.get('uploaded'):
        request('POST',DATA_API+'/richmenu/{}/content'.format(rid),
                data=(ROOT/FILE[brand]).read_bytes(),
                headers={'Content-Type':'image/png'})
        entry['uploaded']=True
        save_state(state)
    alias=ALIAS[brand]
    # Existing alias can be retargeted without changing the sender's user ID.
    response=requests.get(API+'/richmenu/alias/'+alias,
                          headers={'Authorization':'Bearer '+TOKEN},timeout=20)
    if response.status_code==404:
        request('POST',API+'/richmenu/alias',json={'richMenuAliasId':alias,'richMenuId':rid})
    elif response.status_code==200:
        if response.json().get('richMenuId') != rid:
            request('POST',API+'/richmenu/alias/'+alias,json={'richMenuId':rid})
    else:
        raise RuntimeError('Alias lookup failed HTTP {}'.format(response.status_code))
    print('{} ready'.format(brand))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['check','install','activate','status'])
    args=parser.parse_args()
    if args.command=='status':
        state=load_state()
        for brand in BRANDS:
            print(brand, 'READY' if state.get(brand,{}).get('uploaded') else 'NOT INSTALLED')
        return
    users=precheck()
    if args.command=='check':
        print('Menu images: OK; Allowed LINE users: {}'.format(len(users)))
        print('Posting is:', 'ENABLED' if os.getenv('SHARED_LINE_ENABLE_POSTING')=='1' else 'DISABLED')
        return
    state=load_state()
    if args.command=='install':
        for brand in BRANDS:
            ensure_menu(brand,state)
        print('Menus installed. No users linked yet. Next: activate')
    elif args.command=='activate':
        if not all(state.get(b,{}).get('uploaded') for b in BRANDS):
            raise RuntimeError('Run install first')
        # Only pre-authorized staff will see the menu. No public default.
        rich_id=state['crach29']['richMenuId']
        for uid in users:
            request('POST',API+'/user/{}/richmenu/{}'.format(uid,rich_id))
            print('Linked user: {}...{}'.format(uid[:3],uid[-4:]))
        print('Activation complete for {} user(s).'.format(len(users)))


if __name__=='__main__':
    main()
