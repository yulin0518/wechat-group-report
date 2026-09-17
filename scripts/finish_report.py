"""总结写好之后的一步收尾：渲染 PNG / HTML / Markdown，并校验本次产物。

把原来「render_report.py + verify_run.py」两步合并，便于复用：
    python scripts/finish_report.py                        # 自动选最近一次 run
    python scripts/finish_report.py --run-dir reports/<run>

只交付完整长图 report.png；这里不会生成分图（分图需显式运行 split_png.py --force）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_settings() -> dict:
    path = ROOT / 'settings.local.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def latest_run(output_root: Path) -> Path | None:
    if not output_root.exists():
        return None
    runs = [d for d in output_root.iterdir()
            if d.is_dir() and not d.name.startswith('.') and (d / 'messages.json').exists()]
    if not runs:
        return None
    return max(runs, key=lambda d: (d / 'messages.json').stat().st_mtime)


def run_script(name: str, *args) -> int:
    cmd = [sys.executable, str(ROOT / 'scripts' / name), *args]
    return subprocess.run(cmd).returncode


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--run-dir', type=Path, help='本次 run 目录；省略则取输出根目录下最新一次')
    p.add_argument('--output-root', type=Path, help='覆盖配置里的报告输出根目录')
    args = p.parse_args()

    run_dir = args.run_dir
    if not run_dir:
        settings = load_settings()
        output_root = args.output_root or Path(settings.get('output_root', Path.cwd() / 'wechat-reports'))
        run_dir = latest_run(output_root)
        if not run_dir:
            print(f'在 {output_root} 下没有找到含 messages.json 的 run 目录。', file=sys.stderr)
            return 1
        print(f'自动选择最近一次 run：{run_dir.name}')
    run_dir = run_dir.resolve()
    if not (run_dir / 'messages.json').exists():
        print(f'{run_dir} 中没有 messages.json，无法渲染。', file=sys.stderr)
        return 1

    if not (run_dir / 'report.json').exists():
        print(f'缺少 {run_dir}/report.json：请先按 references/report-schema.md 写好总结再运行本脚本。',
              file=sys.stderr)
        return 1

    if run_script('render_report.py', '--run-dir', str(run_dir)) != 0:
        print('渲染失败。', file=sys.stderr)
        return 1
    if run_script('verify_run.py', '--run-dir', str(run_dir)) != 0:
        print('校验未通过，请根据上面的提示修正 report.json 后重跑。', file=sys.stderr)
        return 1

    print('\n交付文件：')
    for name in ('report.png', 'index.html', 'summary.md', 'report.json',
                 'messages.json', 'messages.txt'):
        path = run_dir / name
        print(f'  {"✓" if path.exists() else "✗"} {path}')
    print('\n提示：默认只交付完整长图 report.png；如需编号分图，'
          '显式运行 split_png.py --run-dir "<RUN_DIR>" --force。')
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
