"""把过长的 report.png 按固定高度切分为带编号的分图，便于在聊天窗口查看。

不修改原图，只额外输出 report_partNN.png。切分点落在空白行附近（若可检测到），
否则按最大高度硬切；无论哪种方式都不裁掉任何内容。

注意：本用户默认只要**完整长图**，分图属于例外。因此脚本在缺少 --force 时
直接拒绝执行并提示，只有用户明确要求分图时才带 --force 运行。

用法：
    python scripts/split_png.py --run-dir reports/<run> --force [--max-height 4000]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image


def blank_rows(im: Image.Image, top: int, bottom: int) -> list:
    """在 [top, bottom) 内找出「整行接近背景色」的候选切分位置。"""
    gray = im.convert('L')
    w, h = gray.size
    px = gray.load()
    rows = []
    sample = range(0, w, max(1, w // 60))
    for y in range(top, bottom):
        values = [px[x, y] for x in sample]
        if min(values) >= 240:  # 该行几乎全白
            rows.append(y)
    return rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--max-height', type=int, default=4000)
    p.add_argument('--only-if-taller-than', type=int, default=5600)
    p.add_argument('--force', action='store_true',
                   help='确认用户在本次明确要求分图后再加此参数')
    args = p.parse_args()

    if not args.force:
        print('默认只交付完整长图 report.png，不做分图。\n'
              '若用户本次明确要求分图，请加 --force 重新运行。', file=sys.stderr)
        return 2

    src = (args.run_dir / 'report.png').resolve()
    if not src.exists():
        print('找不到 report.png：' + str(src), file=sys.stderr)
        return 1
    with Image.open(src) as im:
        w, h = im.size
        if h <= args.only_if_taller_than:
            print(f'report.png 高度 {h}px，未超过 {args.only_if_taller_than}px，无需分图。')
            return 0
        parts, start = [], 0
        while start < h:
            limit = min(start + args.max_height, h)
            if limit >= h:
                parts.append((start, h))
                break
            # 在切分点上方 25% 范围内找空白行，避免切在文字中间
            lo = max(start + int(args.max_height * 0.75), start + 1)
            candidates = blank_rows(im, lo, limit)
            cut = candidates[len(candidates) // 2] if candidates else limit
            parts.append((start, cut))
            start = cut
        names = []
        for i, (a, b) in enumerate(parts, 1):
            name = args.run_dir / f'report_part{i:02d}.png'
            im.crop((0, a, w, b)).save(name)
            names.append((name.name, b - a))
    for name, height in names:
        print(f'已生成 {name}（高 {height}px）')
    print(f'原图 {h}px 未做任何裁剪，仅额外输出 {len(names)} 张分图。')
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
