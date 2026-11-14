"""按发送人导出某个微信群在某时间窗口内的全部消息（含图片原图/缩略图）。

与 read_group.py 共用同一套解密与解析实现，只增加两个能力：
1. 按发送人显示名过滤；
2. 对图片消息调用 MediaDownloader 解密落盘（原图缺失时回退缩略图），
   并在输出中标注每张图片的落盘文件。

用法：
    python scripts/export_sender.py --group "群名" --sender "某人" --hours 720
    python scripts/export_sender.py --group "群名" --sender "某人" \
        --end "2026-11-14T18:00:00+08:00" --hours 720 --no-images
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_group import (  # noqa: E402
    ROOT, TZ, cleanup_dir, dependencies, readable_content, sweep_stale,
)


def build_db(database, args, work):
    class ScopedDB(database.WeChatDB):
        def _collect_db_files(self):
            return [item for item in super()._collect_db_files()
                    if item[0].replace('\\', '/') == 'contact/contact.db'
                    or re.fullmatch(r'message/message_\d+\.db', item[0].replace('\\', '/'))]

        def _save_keys(self):
            pass  # 密钥只留在本进程内存

    return ScopedDB(db_dir=args.db_dir, account=args.account, workdir=work)


def collect_rows(db, parser, username, table, start_ts, end_ts):
    rows = []
    wanted = ['local_id', 'local_type', 'server_id', 'real_sender_id',
              'create_time', 'message_content', 'source', 'compress_content',
              'packed_info_data', 'sort_seq']
    for rel in db._message_dbs():
        conn = db._open(rel)
        try:
            integrity = [r[0] for r in conn.execute('PRAGMA quick_check')]
            if integrity != ['ok']:
                raise RuntimeError(f'{rel} 完整性检查未通过：{integrity[:3]}')
            present = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone())
            if not present:
                continue
            cols = parser.table_columns(conn, table)
            stamp = '(CASE WHEN create_time > 10000000000 THEN CAST(create_time / 1000 AS INTEGER) ELSE create_time END)'
            selected = [c for c in wanted if c in cols]
            smap, smap_status = parser._sender_map(conn)
            for raw in conn.execute(
                    f'SELECT {", ".join(selected)} FROM {table} '
                    f'WHERE {stamp} >= ? AND {stamp} < ? ORDER BY create_time, local_id',
                    (start_ts, end_ts)):
                row = dict(raw)
                for c in wanted:
                    row.setdefault(c, None)
                row['_db_rel'] = rel
                sid = parser._sender_id(row['real_sender_id'])
                su = parser._identity_username(smap.get(sid))
                row['_sender_username'] = su
                row['_sender_status'] = 'resolved' if su else smap_status
                rows.append(row)
        finally:
            conn.close()
    rows.sort(key=lambda r: (parser._normalize_timestamp(r['create_time']),
                             r.get('sort_seq') or 0, r.get('local_id') or 0))
    return rows


def save_image(db, dl, index, user, local_id, aes_key, xor_key, img_dir, seq):
    """解密并保存一条图片消息；原图缺失时回退缩略图。返回 (相对路径, 标注)。"""
    row = db.get_message_row(user, local_id)
    if not row or row.get('local_type') not in (3,):
        return None
    md5 = dl._img_md5(row)
    if not md5:
        return None
    dat = index.get(md5 + '.dat')
    kind = '原图'
    if not dat:
        dat = index.get(md5 + '_t.dat')
        kind = '缩略图'
    if not dat:
        return None
    data = dl.decrypt_image(dat, aes_key, xor_key)
    if data[:3] == b'\xff\xd8\xff':
        ext = 'jpg'
    elif data[:4] == b'\x89PNG':
        ext = 'png'
    elif data[:3] == b'GIF':
        ext = 'gif'
    elif data[:4] == b'wxgf':
        jpg = dl._wxgf_to_jpg(data)
        if jpg is None:
            ext, data = 'wxgf', data
        else:
            ext, data = 'jpg', jpg
    else:
        ext = 'img'
    name = f'{seq:04d}_{md5}{"_thumb" if kind == "缩略图" else ""}.{ext}'
    (img_dir / name).write_bytes(data)
    return f'images/{name}', kind


def run(args):
    database, parser = dependencies()
    supplied_end = datetime.fromisoformat(args.end) if args.end else datetime.now(TZ)
    if supplied_end.tzinfo is None:
        raise ValueError('--end 必须包含时区，例如 2026-11-14T18:00:00+08:00')
    end = supplied_end.astimezone(TZ).replace(microsecond=0)
    start = end - timedelta(hours=args.hours)
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())

    out = Path(args.out).expanduser().resolve() if args.out else Path(
        args.output_root).expanduser().resolve() / end.strftime('%Y%m%d-%H%M%S') \
        / f'sender-{args.sender}'
    out.mkdir(parents=True, exist_ok=True)
    img_dir = out / 'images'
    if not args.no_images:
        img_dir.mkdir(exist_ok=True)

    private_root = Path(args.output_root).expanduser().resolve() / '.private'
    private_root.mkdir(parents=True, exist_ok=True)
    for stale in sweep_stale(private_root):
        print('已清理上次遗留的解密临时目录：' + stale, flush=True)
    work = tempfile.mkdtemp(prefix='read-', dir=str(private_root))

    print(f'读取窗口：{start.isoformat()} 至 {end.isoformat()}（含起点、不含终点）', flush=True)
    db = None
    try:
        db = build_db(database, args, work)
        if db.unkeyed:
            raise RuntimeError('所需数据库未全部取得可用密钥：' + ', '.join(db.unkeyed))
        matches = [x for x in db.search_contact(args.group)
                   if x['username'].endswith('@chatroom')
                   and args.group in (x.get('remark'), x.get('nick_name'))]
        if args.group_id:
            matches = [x for x in matches if x['username'] == args.group_id]
        unique = {x['username']: x for x in matches}
        if len(unique) != 1:
            raise RuntimeError(f'准确匹配到 {len(unique)} 个群，无法唯一确定目标。')
        username = next(iter(unique.values()))['username']
        table = 'Msg_' + hashlib.md5(username.encode()).hexdigest()
        print('已准确匹配群名：' + args.group, flush=True)

        nicks = db._nickname_index()
        rows = collect_rows(db, parser, username, table, start_ts, end_ts)
        print(f'窗口内消息 {len(rows)} 条，正在按发送人筛选…', flush=True)

        seen, messages, sender_names = set(), [], Counter()
        for row in rows:
            ts = parser._normalize_timestamp(row['create_time'])
            identity, strip_prefix = parser.resolve_sender(
                row, True, username, args.group, nicks, db.wxid)
            sender_names[identity.get('sender')] += 1
            if identity.get('sender') != args.sender:
                continue
            content = parser.parse_content(row['local_type'], row['message_content'],
                                           row['compress_content'], group_prefix_strip=strip_prefix)
            server_id = row.get('server_id')
            dedup = ((str(server_id), ts, identity.get('sender_username'), row['local_type'], content)
                     if server_id not in (None, 0, '0', '') else (row['_db_rel'], row['local_id']))
            if dedup in seen:
                continue
            seen.add(dedup)
            low_type = parser.low_type(row['local_type'])
            transcript = parser._voice_transcript(row) if low_type == 34 else ''
            messages.append({
                'id': len(messages) + 1,
                'time': datetime.fromtimestamp(ts, TZ).isoformat(),
                'timestamp': ts,
                'type': '联系人名片' if low_type == 42 else parser.TYPE_LABEL.get(low_type, str(low_type)),
                'type_code': row['local_type'],
                'content': readable_content(content, low_type),
                'transcript': transcript or None,
                'local_id': row['local_id'],
                'server_id': parser._json_message_id(server_id),
                'image': None,
            })

        if not messages:
            near = [n for n in sender_names if n and args.sender in n]
            hint = ('；相近的发送人：' + '、'.join(near[:5])) if near else ''
            raise RuntimeError(f'窗口内没有找到发送人「{args.sender}」的消息{hint}')

        video_count = 0
        if not args.include_video:
            video_count = sum(1 for m in messages if m['type'] == '视频')
            messages = [m for m in messages if m['type'] != '视频']

        # 图片解密落盘
        if not args.no_images:
            from importlib import import_module
            media_mod = import_module('wechatauto.media')
            if not getattr(db, 'cfg_dword', None):
                try:
                    auto = db.extract_master_key()
                    if auto:
                        db.cfg_dword = auto[1]
                except Exception as exc:
                    print('  cfgDword 提取失败：' + str(exc), flush=True)
            downloader = media_mod.MediaDownloader(db, save_dir=str(img_dir))
            key = downloader.detect_image_key()
            aes_key, xor_key = key if key else (None, None)
            if not key:
                print('  ！未取得图片密钥，本次只记录图片标记', flush=True)
            # 一次性索引该群媒体目录，避免每张图都遍历磁盘
            index = {}
            base = os.path.join(db.account_dir, 'msg', 'attach', downloader._chat_md5(username))
            for root, _, files in os.walk(base):
                for f in files:
                    index.setdefault(f, os.path.join(root, f))
            total = sum(1 for m in messages if m['type'] == '图片')
            done = 0
            for m in messages:
                if m['type'] != '图片':
                    continue
                done += 1
                saved = None
                try:
                    saved = save_image(db, downloader, index, username, m['local_id'],
                                       aes_key, xor_key, img_dir, m['id'])
                except Exception as exc:
                    print(f'  图片 {m["id"]} 解密失败：{exc}', flush=True)
                if saved:
                    rel, kind = saved
                    m['image'] = rel
                    m['image_note'] = kind
                    if done % 10 == 0 or done == total:
                        print(f'  图片 {done}/{total} → {rel}', flush=True)
                else:
                    m['image_note'] = '本地未找到图片文件（可能未同步或已清理）'
                    print(f'  图片 {done}/{total} 未找到（id={m["id"]}）', flush=True)

        meta = {
            'group': args.group,
            'group_username': username,
            'sender': args.sender,
            'sender_usernames': sorted({m.get('sender_username') for m in messages if m.get('sender_username')}),
            'start': start.isoformat(),
            'end_exclusive': end.isoformat(),
            'hours': args.hours,
            'message_count': len(messages),
            'type_counts': dict(Counter(m['type'] for m in messages)),
            'first_message_time': messages[0]['time'],
            'last_message_time': messages[-1]['time'],
            'images_saved': sum(1 for m in messages if m.get('image')),
            'skipped_video_count': video_count,
            'media_note': '视频已按要求跳过；图片已解密落盘（原图缺失时为缩略图）；语音仅保留微信自带转写。',
        }
        (out / 'messages.json').write_text(
            json.dumps({**meta, 'messages': messages}, ensure_ascii=False, indent=2), encoding='utf-8')

        write_txt(out / f'{args.sender}-发言记录.txt', meta, messages)
        write_md(out / f'{args.sender}-发言记录.md', meta, messages)
        write_html(out / 'index.html', meta, messages)

        print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
        print('导出完成：' + str(out), flush=True)
    finally:
        db = None
        if cleanup_dir(work):
            print('本次解密临时目录已清理。', flush=True)
        else:
            print('警告：解密临时目录未能删除，请手动清理：' + work, flush=True)


def head_lines(meta):
    lines = [
        f'群聊：{meta["group"]}',
        f'发送人：{meta["sender"]}',
        f'时间范围：{meta["start"]} 至 {meta["end_exclusive"]}（含起点、不含终点，UTC+8）',
        f'消息条数：{meta["message_count"]}（' + '、'.join(f'{k} {v}' for k, v in meta['type_counts'].items()) + '）',
        f'实际首条：{meta["first_message_time"]}　末条：{meta["last_message_time"]}',
    ]
    if meta.get('skipped_video_count'):
        lines.append(f'已按要求跳过 {meta["skipped_video_count"]} 条视频消息（编号因此不连续）')
    if meta.get('images_saved'):
        lines.append(f'图片已保存 {meta["images_saved"]} 张至 images/ 目录')
    lines.append('')
    return lines


def write_txt(path, meta, messages):
    lines = ['# 群聊发言记录（纯文本）', ''] + head_lines(meta)
    for m in messages:
        tag = m['type']
        if m['image']:
            tag += f'（{m["image"]}）'
        elif m['type'] == '图片':
            tag += f'（{m.get("image_note") or "未保存"}）'
        lines += [f'[{m["id"]:04d}] {m["time"]}　[{tag}]', m['content']]
        if m['transcript']:
            lines.append('语音转写：' + m['transcript'])
        lines.append('')
    path.write_text('\n'.join(lines), encoding='utf-8')


def write_md(path, meta, messages):
    lines = ['# 发言记录', ''] + [f'- {x}' for x in head_lines(meta) if x]
    lines.append('')
    for m in messages:
        lines.append(f'### {m["id"]:04d} · {m["time"][5:19].replace("T", " ")} · {m["type"]}')
        lines.append('')
        lines.append(m['content'] if m['content'].strip() else '（空）')
        lines.append('')
        if m['image']:
            lines.append(f'![图片 {m["id"]:04d}]({m["image"]})')
            lines.append('')
        elif m['type'] == '图片':
            lines.append(f'> {m.get("image_note") or "图片未保存"}')
            lines.append('')
        if m['transcript']:
            lines.append('> 语音转写：' + m['transcript'])
            lines.append('')
    path.write_text('\n'.join(lines), encoding='utf-8')


def write_html(path, meta, messages):
    rows = []
    for m in messages:
        body = html.escape(m['content']).replace('\n', '<br>')
        parts = [f'<div class="m"><div class="h"><span class="i">{m["id"]:04d}</span>'
                 f'<span class="t">{html.escape(m["time"][5:19].replace("T", " "))}</span>'
                 f'<span class="y">{html.escape(m["type"])}</span></div>'
                 f'<div class="c">{body}</div>']
        if m['image']:
            parts.append(f'<img src="{html.escape(m["image"])}" loading="lazy" alt="{m["id"]:04d}">')
        elif m['type'] == '图片':
            parts.append(f'<div class="note">{html.escape(m.get("image_note") or "图片未保存")}</div>')
        if m['transcript']:
            parts.append(f'<div class="note">语音转写：{html.escape(m["transcript"])}</div>')
        parts.append('</div>')
        rows.append(''.join(parts))
    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(meta['sender'])} · {html.escape(meta['group'])}</title>
<style>
:root{{color-scheme:light}}
body{{margin:0;padding:24px 16px 60px;background:#f5f5f4;color:#1c1917;
font:16px/1.75 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}}
.wrap{{max-width:860px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 6px}}
.sub{{color:#57534e;font-size:13px;line-height:1.9;margin-bottom:18px;
border-left:3px solid #a8a29e;padding-left:12px}}
.m{{background:#fff;border:1px solid #e7e5e4;border-radius:10px;padding:14px 16px;margin:12px 0;
box-shadow:0 1px 2px rgba(0,0,0,.03)}}
.h{{display:flex;gap:10px;align-items:baseline;color:#78716c;font-size:12px;margin-bottom:6px}}
.i{{font-weight:700;color:#b45309;font-variant-numeric:tabular-nums}}
.y{{margin-left:auto;background:#f5f5f4;border-radius:4px;padding:1px 6px;color:#57534e}}
.c{{white-space:normal;word-break:break-word}}
img{{max-width:100%;height:auto;display:block;margin-top:10px;border-radius:6px;border:1px solid #e7e5e4}}
.note{{font-size:13px;color:#a16207;margin-top:8px}}
</style></head><body><div class="wrap">
<h1>{html.escape(meta['sender'])} · 发言记录</h1>
<div class="sub">{'<br>'.join(html.escape(x) for x in head_lines(meta) if x)}</div>
{''.join(rows)}
</div></body></html>"""
    path.write_text(doc, encoding='utf-8')


def main(argv=None):
    settings_path = ROOT / 'settings.local.json'
    settings = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else {}
    p = argparse.ArgumentParser()
    p.add_argument('--group', required=True)
    p.add_argument('--group-id')
    p.add_argument('--sender', required=True, help='发送人在群里的显示名（需完全一致）')
    p.add_argument('--db-dir', default=settings.get('db_dir'))
    p.add_argument('--account', default=settings.get('account'))
    p.add_argument('--hours', type=float, default=720)
    p.add_argument('--end')
    p.add_argument('--output-root', default=settings.get('output_root', str(Path.cwd() / 'wechat-reports')))
    p.add_argument('--out', help='直接指定输出目录')
    p.add_argument('--no-images', action='store_true', help='只导出文字，不下载图片')
    p.add_argument('--include-video', action='store_true', help='保留视频消息标记（默认跳过）')
    args = p.parse_args(argv)
    if not math.isfinite(args.hours) or args.hours <= 0:
        p.error('--hours 必须是大于 0 的有限数值')
    if os.name != 'nt':
        p.error('当前打包的读取器仅支持 Windows 微信 4.x')
    if not args.account:
        p.error('请明确 --account 或在 settings.local.json 中配置')
    run(args)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
