"""列出本机当前账号可读的群聊，供选择要总结的目标群。

只读 contact.db / session.db（如可用）；不改动任何原始数据库。
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_group import cleanup_dir, dependencies, sweep_stale  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


def run(args):
    database, _parser = dependencies()

    class ScopedDB(database.WeChatDB):
        def _collect_db_files(self):
            keep = ('contact/contact.db', 'session/session.db')
            return [item for item in super()._collect_db_files()
                    if item[0].replace('\\', '/') in keep
                    or re.fullmatch(r'message/message_\d+\.db', item[0].replace('\\', '/'))]

        def _save_keys(self):
            pass

    private_root = Path(args.output_root).resolve() / '.private'
    private_root.mkdir(parents=True, exist_ok=True)
    for stale in sweep_stale(private_root):
        print('已清理上次遗留的解密临时目录：' + stale, flush=True)
    work = tempfile.mkdtemp(prefix='read-', dir=str(private_root))
    # 无论正常退出还是抛异常，都保证解密副本被删除。
    atexit.register(cleanup_dir, work)
    db = ScopedDB(db_dir=args.db_dir, account=args.account, workdir=work)
    if db.unkeyed:
        print('未取得密钥：' + ', '.join(db.unkeyed), file=sys.stderr)

    # 自身信息
    try:
        self_info = db.get_self_info()
        print('当前账号：%s (%s)' % (db.wxid, self_info.get('nick_name') or self_info.get('remark') or '未命名'))
    except Exception as exc:  # noqa: BLE001
        print('自身信息读取失败：%s' % exc, file=sys.stderr)

    def rel_of(basename):
        for rel, path, _ in db._db_files:
            if os.path.basename(path) == basename:
                return rel
        return None

    last_seen = {}
    session_rel = rel_of('session.db')
    if session_rel:
        try:
            conn = db._open(session_rel)
            try:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                for t in tables:
                    cols = {r[1] for r in conn.execute(f'PRAGMA table_info({t})')}
                    if 'username' not in cols:
                        continue
                    time_cols = [c for c in ('last_timestamp', 'timestamp', 'sort_timestamp',
                                             'last_msg_time', 'update_time') if c in cols]
                    if not time_cols:
                        continue
                    tc = time_cols[0]
                    for r in conn.execute(f'SELECT username, {tc} FROM {t}'):
                        try:
                            ts = int(r[1])
                        except (TypeError, ValueError):
                            continue
                        if ts > 10 ** 10:
                            ts //= 1000
                        cur = last_seen.get(r[0])
                        if cur is None or ts > cur:
                            last_seen[r[0]] = ts
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            print('会话表读取跳过：%s' % exc, file=sys.stderr)

    contact_rel = rel_of('contact.db')
    conn = db._open(contact_rel)
    try:
        rows = conn.execute(
            "SELECT username, nick_name, remark FROM contact "
            "WHERE username LIKE '%@chatroom'").fetchall()
    finally:
        conn.close()

    groups = []
    for r in rows:
        name = (r['remark'] or '').strip() or (r['nick_name'] or '').strip()
        if not name:
            continue
        ts = last_seen.get(r['username'])
        groups.append({
            'name': name,
            'username': r['username'],
            'nick_name': r['nick_name'],
            'remark': r['remark'],
            'last_active': datetime.fromtimestamp(ts, TZ).isoformat() if ts else None,
            'last_ts': ts or 0,
        })

    named = {}
    for g in groups:
        named.setdefault(g['name'], []).append(g)
    duplicates = {n: len(v) for n, v in named.items() if len(v) > 1}

    groups.sort(key=lambda g: (-g['last_ts'], g['name']))
    print('共 %d 个群（可唯一匹配群名 %d 个）' % (len(groups), len(named)))
    if duplicates:
        print('同名群（需用 group-id 消歧）：')
        for n, c in duplicates.items():
            print('  %s × %d' % (n, c))
    print('')
    for g in groups if args.all else groups[:args.limit]:
        flag = ' [同名]' if duplicates.get(g['name']) else ''
        print('%s\t%s\t%s' % (g['last_active'] or '无会话记录', g['name'] + flag, g['username']))
    if args.json:
        Path(args.json).write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding='utf-8')
        print('\n已写出：' + args.json)

    import gc
    del db
    gc.collect()
    print('本次解密临时目录已清理。' if cleanup_dir(work) else '警告：临时目录未能删除：' + work)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    settings_path = ROOT / 'settings.local.json'
    settings = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else {}
    p = argparse.ArgumentParser()
    p.add_argument('--db-dir', default=settings.get('db_dir'))
    p.add_argument('--account', default=settings.get('account'))
    p.add_argument('--output-root', default=settings.get('output_root', str(ROOT / 'reports')))
    p.add_argument('--limit', type=int, default=40)
    p.add_argument('--all', action='store_true')
    p.add_argument('--json')
    args = p.parse_args()
    run(args)
