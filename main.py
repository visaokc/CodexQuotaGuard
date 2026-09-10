import argparse
import ctypes
import hashlib
import os
import sys
from pathlib import Path


def default_folder():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CodexQuotaGuard'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=default_folder())
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--no-autostart', action='store_true', help='Run without changing startup registration')
    parser.add_argument('--smoke-seconds', type=int, default=0)
    parser.add_argument('--update-health', type=Path)
    parser.add_argument('--update-nonce')
    args = parser.parse_args()
    if os.name == 'nt':
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, False, 'Local\\CodexQuotaGuard-'+hashlib.sha256(str(args.data_dir.resolve()).encode()).hexdigest()[:16])
        if ctypes.get_last_error() == 183:
            if not args.background:
                ctypes.windll.user32.MessageBoxW(None, '配额管家已在运行，请从系统托盘打开主窗口。', 'Codex 配额管家', 0)
            return
    from quota_guard.storage import Database, defaults
    from quota_guard.pairing import load_config, save_config
    args.data_dir.mkdir(parents=True, exist_ok=True)
    cfgpath = args.data_dir/'settings.json'
    config = defaults()
    if cfgpath.exists():
        config.update(load_config(cfgpath))
    else:
        save_config(cfgpath, config)
    if not config.get("personal_display_default_v1"):
        config.update(quota_display="personal", personal_display_default_v1=True)
        save_config(cfgpath, config)
    if not config.get('shared_group_prepare_v1'):
        config.update(shared_group_enabled=True, shared_group_prepare_v1=True, auto_block=False)
        scope = 'group:'+hashlib.sha256(config['group_secret'].encode()).hexdigest()[:20]
        for field in ('device_notes', 'device_colors', 'device_order'):
            settings = config.setdefault(field, {})
            if scope not in settings:
                for account in config.get('tracked_accounts', {}):
                    if settings.get(account):
                        settings[scope] = settings[account].copy()
                        break
        save_config(cfgpath, config)
    if not config.get('shared_billing_v1'):
        config.update(shared_billing_v1=True, auto_block=False)
        save_config(cfgpath, config)
    from quota_guard.web_host import run
    run(args, config, Database(args.data_dir/'local.sqlite'))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, str(e), 'Codex 配额管家启动失败', 0x10)
        raise
