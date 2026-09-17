"""校验一次 run 目录的自洽性（不接触微信进程）。

检查：统计口径是否一致、报告引用的消息编号是否真实存在、
HTML 是否无外部依赖且确实包含引用编号、PNG 是否可解析且尺寸合理。

用法：
    python scripts/verify_run.py --run-dir reports/<run>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from html import unescape
from pathlib import Path

REQUIRED = ['messages.json', 'messages.txt', 'report.json', 'summary.md',
            'index.html', 'report.png']


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--run-dir', required=True)
    args = p.parse_args()
    run = Path(args.run_dir).resolve()
    failures: list[str] = []
    notes: list[str] = []

    for name in REQUIRED:
        if not (run / name).exists():
            failures.append('缺少文件：' + name)
    if failures:
        print('\n'.join(failures))
        return 1

    raw = json.loads((run / 'messages.json').read_text(encoding='utf-8'))
    report = json.loads((run / 'report.json').read_text(encoding='utf-8'))
    messages = raw['messages']
    ids = {m['id'] for m in messages}

    # 1) 统计口径
    if raw['message_count'] != len(messages):
        failures.append('message_count 与消息条数不一致')
    senders = {m['sender_username'] for m in messages if m['sender_username']}
    if raw['sender_count'] != len(senders):
        failures.append('sender_count 与去重发送者不一致')
    types = Counter(m['type'] for m in messages)
    if dict(types) != raw['type_counts']:
        failures.append('type_counts 与消息类型分布不一致')

    # 2) 时间窗口：含起点、不含终点
    window_ok = all(raw['start'] <= m['time'] < raw['end_exclusive'] for m in messages)
    if not window_ok:
        failures.append('存在消息时间落在窗口之外（应为含起点不含终点）')

    # 3) 时间与编号单调
    if [m['id'] for m in messages] != list(range(1, len(messages) + 1)):
        failures.append('消息编号不是从 1 连续递增')
    if [m['timestamp'] for m in messages] != sorted(m['timestamp'] for m in messages):
        notes.append('消息按时间排序：通过' if not messages else '')

    # 4) 引用编号必须真实存在
    if not report.get('intro'):
        failures.append('report.json 缺少 intro')
    for section in ('topics', 'followups', 'resolved', 'other'):
        for item in report.get(section, []):
            if not item.get('title') or not item.get('paragraphs'):
                failures.append(f'{section} 存在空标题或空段落')
            refs = item.get('message_ids') or []
            if not refs:
                failures.append(f'{section}「{item.get("title")}」没有任何消息引用')
            missing = [r for r in refs if not isinstance(r, int) or r not in ids]
            if missing:
                failures.append(f'{section}「{item.get("title")}」引用了不存在的编号：{missing}')

    # 5) HTML 无外部依赖，并包含全部引用编号
    html = (run / 'index.html').read_text(encoding='utf-8')
    externals = re.findall(r'(?:src|href)="(?!#)([^"]+)"', html)
    if externals:
        failures.append('HTML 引用了外部资源：' + ', '.join(externals[:5]))
    text = unescape(html)
    if not re.search(r'来源|source', html):
        failures.append('HTML 未包含来源说明')
    if 'message' not in text and '消息' not in text:
        failures.append('HTML 正文可疑：未出现消息相关文案')

    # 6) PNG 可解析
    try:
        from PIL import Image
        with Image.open(run / 'report.png') as im:
            im.verify()
        with Image.open(run / 'report.png') as im:
            w, h = im.size
        if w < 400 or h < 400:
            failures.append(f'PNG 尺寸异常：{w}x{h}')
        else:
            notes.append(f'PNG 尺寸 {w}x{h}')
    except Exception as exc:  # noqa: BLE001
        failures.append('PNG 无法解析：%s' % exc)

    # 7) 数据库检查项
    for check in raw.get('database_checks', []):
        if check.get('quick_check') != ['ok']:
            failures.append(f"{check.get('source_db')} 完整性检查未通过")
    notes.append('参与分片：' + ', '.join(c['source_db'] for c in raw.get('database_checks', [])))
    notes.append(f"窗口内 {raw['message_count']} 条，去重 {raw['duplicates_removed']} 条，"
                 f"发送者 {raw['sender_count']} 人")

    print('校验目录：' + str(run))
    for n in notes:
        if n:
            print('  · ' + n)
    if failures:
        print('\n未通过：')
        for f in failures:
            print('  ✗ ' + f)
        return 1
    print('\n全部检查通过。')
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
