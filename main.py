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
    from quota_guard.gui import App
    import customtkinter as ctk
    args.data_dir.mkdir(parents=True, exist_ok=True)
    cfgpath = args.data_dir/'settings.json'
    config = defaults()
    if cfgpath.exists():
        config.update(load_config(cfgpath))
    else:
        save_config(cfgpath, config)
    ctk.set_appearance_mode('dark')
    root = ctk.CTk()
    app = App(root, args.data_dir, config, Database(args.data_dir/'local.sqlite'), demo=args.demo,
              startup_enabled=not args.demo and not args.smoke_seconds)
    if args.background:
        root.after(100, app.close)
    if args.smoke_seconds:
        root.after(args.smoke_seconds*1000, app.quit)
    if args.update_health and args.update_nonce:
        from quota_guard import __version__
        from quota_guard.storage import atomic_json
        expected = args.data_dir.resolve()/'updates'/(args.update_nonce+'.health')
        if args.update_health.resolve() == expected and len(args.update_nonce) == 48:
            root.after(1500, lambda: atomic_json(expected, dict(nonce=args.update_nonce, version=__version__)))
    root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, str(e), 'Codex 配额管家启动失败', 0x10)
        raise
