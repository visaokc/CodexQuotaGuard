import argparse
import ctypes
import os
import time
from pathlib import Path

from quota_guard.firewall import Firewall, is_admin
from quota_guard.storage import Database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('LOCALAPPDATA', str(Path.home())))/'CodexQuotaGuard')
    args = parser.parse_args()
    if not is_admin():
        raise RuntimeError('请以管理员身份运行恢复工具')
    fw = Firewall()
    db = Database(args.data_dir/'local.sqlite')
    db.put('manual_restore_at', time.time())
    fw.resume(db)
    fw.restore()
    db.put('block_state', None)
    cfg = args.data_dir/'settings.json'
    if cfg.exists():
        from quota_guard.pairing import load_config, save_config
        settings = load_config(cfg)
        settings['auto_block'] = False
        save_config(cfg, settings)
    ctypes.windll.user32.MessageBoxW(None, '已恢复本工具暂停的进程、撤销本工具的防火墙规则，并停用自动限制。', 'Codex 网络恢复', 0x40)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        ctypes.windll.user32.MessageBoxW(None, str(e), '恢复失败', 0x10)
