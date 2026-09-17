"""首次使用 / 换机复用：准备运行环境并生成本机配置。

做三件事：
  1. 在当前技能目录创建独立 venv 并安装 scripts/requirements.txt 里的依赖；
  2. 探测本机微信数据目录与当前活跃账号（按消息库最后修改时间判断）；
  3. 写出 settings.local.json（已存在时不覆盖，除非加 --force）。

写出的配置只有路径、账号目录名和显示名，不含任何密钥；密钥只在读取时
从微信进程内存短暂取用，不落盘。

用法：
    python scripts/bootstrap.py                      # 自动探测 + 安装依赖
    python scripts/bootstrap.py --self-name "我的群昵称" --group "常用群名"
    python scripts/bootstrap.py --db-dir "D:/xwechat_files" --account "wxid_xxx" --force
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DB_PARENTS = [
    Path.home() / 'Documents' / 'xwechat_files',
    Path.home() / 'Documents' / 'WeChat Files',
    Path('C:/Users') / os.environ.get('USERNAME', '') / 'Documents' / 'xwechat_files',
]


def find_db_dir(explicit=None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    for candidate in DEFAULT_DB_PARENTS:
        if candidate.exists():
            return candidate
    return None


def account_dirs(db_dir: Path) -> list:
    """返回 (账号目录名, 消息库最后修改时间, 消息库数量)。"""
    found = []
    for child in sorted(db_dir.iterdir()):
        if not child.is_dir():
            continue
        stamp, count = 0.0, 0
        for db in child.rglob('message_*.db'):
            count += 1
            try:
                stamp = max(stamp, db.stat().st_mtime)
            except OSError:
                pass
        if count:
            found.append((child.name, stamp, count))
    found.sort(key=lambda item: item[1], reverse=True)
    return found


def ensure_venv(force: bool = False) -> Path | None:
    """创建/复用技能自带 venv，并确保依赖可用。返回 venv 内的 python 路径。"""
    py = ROOT / '.venv' / 'Scripts' / 'python.exe'
    if not py.exists():
        print('创建技能专用 venv：' + str(ROOT / '.venv'))
        base = sys.executable or 'python'
        if subprocess.run([base, '-m', 'venv', str(ROOT / '.venv')]).returncode != 0:
            print('创建 venv 失败，将改用当前解释器。', file=sys.stderr)
            return None
    probe = subprocess.run([str(py), '-c', 'import cryptography, zstandard, PIL'],
                           capture_output=True, text=True)
    if probe.returncode != 0 or force:
        print('安装依赖：scripts/requirements.txt')
        if subprocess.run([str(py), '-m', 'pip', 'install', '--disable-pip-version-check',
                           '-q', '-r', str(ROOT / 'scripts' / 'requirements.txt')]).returncode != 0:
            print('依赖安装失败，请检查网络后重试。', file=sys.stderr)
            return None
    check = subprocess.run([str(py), '-c', 'import cryptography, zstandard, PIL;'
                            'print("依赖就绪:", cryptography.__version__)'],
                           capture_output=True, text=True)
    print(check.stdout.strip() or check.stderr.strip())
    return py


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--db-dir', help='微信数据目录（含 wxid_* 账号子目录）')
    p.add_argument('--account', help='账号目录名；默认取消息库最新更新的那个')
    p.add_argument('--self-name', default='', help='你在群里的显示名')
    p.add_argument('--group', default='', help='常用群名（可留空）')
    p.add_argument('--output-root', help='报告输出根目录')
    p.add_argument('--force', action='store_true', help='覆盖已存在的 settings.local.json')
    p.add_argument('--skip-deps', action='store_true', help='只写配置，不动 venv')
    args = p.parse_args()

    settings_path = ROOT / 'settings.local.json'
    existing = {}
    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding='utf-8'))
        if not args.force:
            print('settings.local.json 已存在（用 --force 覆盖）。当前内容：')
            print(json.dumps(existing, ensure_ascii=False, indent=2))
            print('\n如需只更新单项，直接编辑该文件即可。')
            return 0

    if not args.skip_deps:
        ensure_venv()

    db_dir = find_db_dir(args.db_dir or existing.get('db_dir'))
    if not db_dir:
        print('未找到微信数据目录，请用 --db-dir 指定。', file=sys.stderr)
        return 1
    accounts = account_dirs(db_dir)
    if not accounts:
        print(f'{db_dir} 下没有找到任何含 message_*.db 的账号目录。', file=sys.stderr)
        return 1

    account = args.account or existing.get('account')
    if account and account not in {name for name, _, _ in accounts}:
        print(f'指定的账号目录不存在：{account}', file=sys.stderr)
        return 1
    if not account:
        account = accounts[0][0]
        if len(accounts) > 1:
            print('检测到多个账号，已按消息库最后更新时间选中最新活跃的一个：')
            for name, stamp, count in accounts:
                when = datetime.fromtimestamp(stamp, timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M') if stamp else '未知'
                print(f'  {"→" if name == account else " "} {name}  最后更新 {when}  分片 {count}')
            print('若应使用其他账号，请加 --account 重新运行；脚本不会自动混读多个账号。')

    settings = {
        'db_dir': str(db_dir).replace('\\', '/'),
        'account': account,
        'self_name': args.self_name or existing.get('self_name', ''),
        'output_root': str(Path(args.output_root).expanduser() if args.output_root
                           else existing.get('output_root')
                           or (Path.home() / 'Documents' / 'wechat-reports')).replace('\\', '/'),
        'default_group': args.group or existing.get('default_group', ''),
    }
    Path(settings['output_root']).mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8')
    print('\n已写入 ' + str(settings_path))
    print(json.dumps(settings, ensure_ascii=False, indent=2))
    print('\n下一步：python scripts/list_groups.py --limit 30   查看群名与最近活跃时间')
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
