#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宝宝喂养记录 —— 喝奶数据同步服务（纯标准库，无第三方依赖）

存储规则：每份数据一个 JSON 文件，按数据主人的微信 openid 分桶到子文件夹：
    data/<bucket>/<openid>.json
bucket 取 openid 中出现的前 4 个数字字符（如 ff345k54... -> 3455/），
不足 4 位时用 md5(openid) 中的数字补齐。

家庭共享：用户 A 生成共享码发给用户 B，B 绑定后，B 的上传/下载
全部指向 A 的数据文件，两人共同维护同一份数据。
上传采用「按记录 id 合并」而非整体覆盖，并用 deletedIds 墓碑避免
已删除的记录被另一方的旧数据复活。

接口：
    GET  /api/health                            健康检查
    POST /api/login     {"code"}                wx.login 的 code 换 openid，返回 {openid, token}
                        {"devId"}               DEV_MODE=1 时可用，本地调试免微信登录
    POST /api/sync/upload   {"openid","token","payload"}  合并上传，返回合并后的完整 payload
    GET  /api/sync/download?openid=..&token=..            下载完整备份
    POST /api/share/code    {"openid","token"}             获取（没有则生成）我的共享码
    POST /api/share/bind    {"openid","token","shareCode"} 绑定他人共享码
    POST /api/share/unbind  {"openid","token"}             解绑
    GET  /api/share/info?openid=..&token=..                查询绑定状态

环境变量：
    PORT          监听端口，默认 8300
    DATA_DIR      数据目录，默认 ./data
    WX_APPID      小程序 AppID，默认取自 manifest（wx5e292c7a374e892d）
    WX_SECRET     小程序 AppSecret（必填，否则 /api/login 只能走 DEV_MODE）
    TOKEN_SECRET  签发 token 的密钥；不设置则自动生成并保存在 data/.token_secret
    DEV_MODE      置为 1 允许 devId 登录（仅本地联调用，线上不要开）
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'data'))
META_DIR = os.path.join(DATA_DIR, '_meta')
SHARES_FILE = os.path.join(META_DIR, 'shares.json')      # {共享码: 数据主人openid}
BINDINGS_FILE = os.path.join(META_DIR, 'bindings.json')  # {成员openid: 数据主人openid}
PORT = int(os.environ.get('PORT', '8300'))
WX_APPID = os.environ.get('WX_APPID', 'wx5e292c7a374e892d')
WX_SECRET = os.environ.get('WX_SECRET', '')
DEV_MODE = os.environ.get('DEV_MODE', '') == '1'
MAX_BODY = 2 * 1024 * 1024  # 单次上传上限 2MB

OPENID_RE = re.compile(r'^[A-Za-z0-9_\-]{4,64}$')
# 共享码字符集去掉易混淆的 0/O/1/I
CODE_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'
CODE_LEN = 6

LOCK = threading.Lock()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('feeding-sync')


def _load_token_secret() -> bytes:
    env = os.environ.get('TOKEN_SECRET', '')
    if env:
        return env.encode('utf-8')
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, '.token_secret')
    if os.path.exists(path):
        with open(path, 'rb') as f:
            return f.read().strip()
    secret = secrets.token_hex(32).encode('ascii')
    with open(path, 'wb') as f:
        f.write(secret)
    log.info('generated new TOKEN_SECRET at %s', path)
    return secret


TOKEN_SECRET = _load_token_secret()


def make_token(openid: str) -> str:
    return hmac.new(TOKEN_SECRET, openid.encode('utf-8'), hashlib.sha256).hexdigest()


def check_token(openid: str, token: str) -> bool:
    return hmac.compare_digest(make_token(openid), token or '')


def bucket_of(openid: str) -> str:
    """取 openid 中前 4 个数字作为子文件夹名，如 ff345k54xx -> 3455"""
    digits = ''.join(c for c in openid if c.isdigit())
    if len(digits) < 4:
        digits += ''.join(c for c in hashlib.md5(openid.encode('utf-8')).hexdigest() if c.isdigit())
    return (digits + '0000')[:4]


def user_file(openid: str) -> str:
    folder = os.path.join(DATA_DIR, bucket_of(openid))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, openid + '.json')


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        log.exception('read %s failed', path)
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def resolve_owner(openid: str) -> str:
    """成员绑定过共享码时，返回数据主人的 openid；否则就是自己"""
    bindings = _read_json(BINDINGS_FILE, {})
    return bindings.get(openid, openid)


def get_or_create_share_code(openid: str) -> str:
    with LOCK:
        shares = _read_json(SHARES_FILE, {})
        for code, owner in shares.items():
            if owner == openid:
                return code
        while True:
            code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            if code not in shares:
                break
        shares[code] = openid
        _write_json(SHARES_FILE, shares)
        return code


def merge_payload(old, new, now_ms):
    """按记录 id 合并两份备份；deletedIds 为删除墓碑，settings 以本次上传为准"""
    old = old if isinstance(old, dict) else {}
    deleted = set(old.get('deletedIds') or []) | set(new.get('deletedIds') or [])
    by_id = {}
    for r in (old.get('records') or []):
        rid = r.get('id') if isinstance(r, dict) else None
        if rid:
            by_id[rid] = r
    for r in (new.get('records') or []):
        rid = r.get('id') if isinstance(r, dict) else None
        if rid:
            by_id[rid] = r  # 同 id 以上传方为准（编辑过的记录）
    records = [r for rid, r in by_id.items() if rid not in deleted]
    records.sort(key=lambda r: r.get('timestamp') or 0, reverse=True)
    return {
        'version': 2,
        'exportedAt': now_ms,
        'records': records,
        'settings': new.get('settings') or old.get('settings'),
        'deletedIds': sorted(deleted),
    }


def wx_code2session(code: str):
    """调微信 jscode2session，返回 (openid, err)"""
    if not WX_SECRET:
        return None, '服务器未配置 WX_SECRET'
    url = (
        'https://api.weixin.qq.com/sns/jscode2session'
        '?appid=%s&secret=%s&js_code=%s&grant_type=authorization_code'
        % (urllib.parse.quote(WX_APPID), urllib.parse.quote(WX_SECRET), urllib.parse.quote(code))
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return None, '请求微信接口失败: %s' % e
    openid = data.get('openid')
    if not openid:
        return None, '微信返回错误 %s: %s' % (data.get('errcode'), data.get('errmsg'))
    return openid, None


class Handler(BaseHTTPRequestHandler):
    server_version = 'FeedingSync/1.0'

    # ---------- helpers ----------

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, status=400):
        self._send_json({'ok': False, 'error': msg}, status)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0:
            return None, '请求体为空'
        if length > MAX_BODY:
            return None, '请求体过大'
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode('utf-8')), None
        except Exception:
            return None, 'JSON 解析失败'

    def _auth(self, openid, token):
        """校验 openid 格式与 token，失败时直接响应并返回 False"""
        if not openid or not OPENID_RE.match(openid):
            self._error('openid 非法', 400)
            return False
        if not check_token(openid, token):
            self._error('token 校验失败', 401)
            return False
        return True

    def log_message(self, fmt, *args):
        log.info('%s %s', self.address_string(), fmt % args)

    # ---------- routes ----------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ('/', '/api/health'):
            self._send_json({'ok': True, 'service': 'feeding-sync', 'time': int(time.time() * 1000)})
        elif parsed.path == '/api/sync/download':
            self.handle_download(parsed)
        elif parsed.path == '/api/share/info':
            self.handle_share_info(parsed)
        else:
            self._error('not found', 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        body, err = self._read_json_body()
        if body is None:
            self._error(err)
            return
        if parsed.path == '/api/login':
            self.handle_login(body)
        elif parsed.path == '/api/sync/upload':
            self.handle_upload(body)
        elif parsed.path == '/api/share/code':
            self.handle_share_code(body)
        elif parsed.path == '/api/share/bind':
            self.handle_share_bind(body)
        elif parsed.path == '/api/share/unbind':
            self.handle_share_unbind(body)
        else:
            self._error('not found', 404)

    def handle_login(self, body):
        dev_id = str(body.get('devId') or '')
        code = str(body.get('code') or '')
        if DEV_MODE and dev_id:
            cleaned = re.sub(r'[^A-Za-z0-9_\-]', '', dev_id)[:56]
            if len(cleaned) < 4:
                self._error('devId 非法')
                return
            openid = 'dev_' + cleaned
        elif code:
            openid, err = wx_code2session(code)
            if openid is None:
                self._error(err, 502)
                return
        else:
            self._error('缺少 code')
            return
        self._send_json({'ok': True, 'openid': openid, 'token': make_token(openid)})

    def handle_upload(self, body):
        openid = str(body.get('openid') or '')
        token = str(body.get('token') or '')
        if not self._auth(openid, token):
            return
        payload = body.get('payload')
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                self._error('payload 不是合法 JSON')
                return
        if not isinstance(payload, dict):
            self._error('缺少 payload')
            return
        now = int(time.time() * 1000)
        owner = resolve_owner(openid)
        path = user_file(owner)
        with LOCK:
            old_doc = _read_json(path, {})
            merged = merge_payload(old_doc.get('payload'), payload, now)
            _write_json(path, {'openid': owner, 'updatedAt': now, 'updatedBy': openid, 'payload': merged})
        count = len(merged['records'])
        log.info('upload openid=%s owner=%s records=%d -> %s',
                 openid, owner, count, os.path.relpath(path, DATA_DIR))
        self._send_json({'ok': True, 'updatedAt': now, 'recordCount': count, 'payload': merged})

    def handle_download(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        openid = (qs.get('openid') or [''])[0]
        token = (qs.get('token') or [''])[0]
        if not self._auth(openid, token):
            return
        owner = resolve_owner(openid)
        path = user_file(owner)
        if not os.path.exists(path):
            self._send_json({'ok': True, 'found': False})
            return
        doc = _read_json(path, None)
        if doc is None:
            self._error('云端数据损坏', 500)
            return
        self._send_json({
            'ok': True,
            'found': True,
            'updatedAt': doc.get('updatedAt', 0),
            'payload': doc.get('payload'),
        })

    def handle_share_code(self, body):
        openid = str(body.get('openid') or '')
        token = str(body.get('token') or '')
        if not self._auth(openid, token):
            return
        bindings = _read_json(BINDINGS_FILE, {})
        if openid in bindings:
            self._error('已绑定他人共享码，解绑后才能生成自己的共享码')
            return
        code = get_or_create_share_code(openid)
        self._send_json({'ok': True, 'code': code})

    def handle_share_bind(self, body):
        openid = str(body.get('openid') or '')
        token = str(body.get('token') or '')
        if not self._auth(openid, token):
            return
        code = str(body.get('shareCode') or '').strip().upper()
        if not code:
            self._error('缺少 shareCode')
            return
        with LOCK:
            shares = _read_json(SHARES_FILE, {})
            owner = shares.get(code)
            if owner is None:
                self._error('共享码不存在')
                return
            bindings = _read_json(BINDINGS_FILE, {})
            owner = bindings.get(owner, owner)  # 拍平链式绑定
            if owner == openid:
                self._error('不能绑定自己的共享码')
                return
            bindings[openid] = owner
            _write_json(BINDINGS_FILE, bindings)
        log.info('bind %s -> %s (code=%s)', openid, owner, code)
        self._send_json({'ok': True, 'code': code})

    def handle_share_unbind(self, body):
        openid = str(body.get('openid') or '')
        token = str(body.get('token') or '')
        if not self._auth(openid, token):
            return
        with LOCK:
            bindings = _read_json(BINDINGS_FILE, {})
            if openid in bindings:
                del bindings[openid]
                _write_json(BINDINGS_FILE, bindings)
        log.info('unbind %s', openid)
        self._send_json({'ok': True})

    def handle_share_info(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        openid = (qs.get('openid') or [''])[0]
        token = (qs.get('token') or [''])[0]
        if not self._auth(openid, token):
            return
        shares = _read_json(SHARES_FILE, {})
        bindings = _read_json(BINDINGS_FILE, {})
        my_code = ''
        for code, owner in shares.items():
            if owner == openid:
                my_code = code
                break
        bound_owner = bindings.get(openid, '')
        bound_code = ''
        if bound_owner:
            for code, owner in shares.items():
                if owner == bound_owner:
                    bound_code = code
                    break
        self._send_json({'ok': True, 'myCode': my_code, 'bound': bound_owner != '', 'boundCode': bound_code})


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    log.info('data dir : %s', DATA_DIR)
    log.info('appid    : %s', WX_APPID)
    log.info('wx secret: %s', '已配置' if WX_SECRET else '未配置（/api/login 仅 DEV_MODE 可用）')
    log.info('dev mode : %s', DEV_MODE)
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    log.info('listening on 0.0.0.0:%d', PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == '__main__':
    main()
