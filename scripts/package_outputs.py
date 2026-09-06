"""Export explicit deliverables only; never include local runtime databases or credentials."""
import hashlib
from pathlib import Path
import shutil
import sys
import zipfile

sys.stdout.reconfigure(encoding='utf-8')

root = Path(__file__).resolve().parents[1]
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
dist = root/'dist'/'0.2.1'
with zipfile.ZipFile(out/'CodexQuotaGuard-0.2.1-Windows.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for p in sorted(dist.rglob('*')):
        if p.is_file():
            z.write(p, 'CodexQuotaGuard/'+p.relative_to(dist).as_posix())
sources = [root/name for name in ('README.md', 'VALIDATION.md', 'THIRD_PARTY_NOTICES.txt',
    'main.py', 'updater_main.py', 'recover_main.py', 'server_main.py', 'build.ps1', 'requirements.txt', 'requirements-dev.txt', '.gitignore')]
for name in ('quota_guard', 'tests', 'scripts', 'deploy', 'assets', 'licenses', 'vendor'):
    sources.extend(p for p in (root/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
with zipfile.ZipFile(out/'CodexQuotaGuard-0.2.1-Source.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for p in sorted(sources):
        assert p.name != '.env' and p.name != 'settings.json' and '.sqlite' not in p.name
        z.write(p, 'CodexQuotaGuard-Source/'+p.relative_to(root).as_posix())
shutil.copy2(dist/'Codex配额管家.exe', out/'CodexQuotaGuard-0.2.1.exe')
for name in ('Codex配额管家.exe', '恢复Codex网络.exe'):
    shutil.copy2(dist/name, out/(Path(name).stem+'-0.2.1.exe'))
shutil.copy2(root/'README.md', out/'使用说明-0.2.1.md')
shutil.copy2(root/'VALIDATION.md', out/'验证记录-0.2.1.md')
for source, name in [('overview.png', 'CodexQuotaGuard-UI-0.2.1.png'), ('cap-dialog.png', '本机限额-0.2.1.png'), ('token-history.png', 'Token统计-0.2.1.png')]:
    shutil.copy2(root/'work'/'ui-preview-0.2.1'/source, out/name)
names = ('CodexQuotaGuard-0.2.1.exe', 'CodexQuotaGuard-0.2.1-Windows.zip', 'CodexQuotaGuard-0.2.1-Source.zip', 'Codex配额管家-0.2.1.exe', '恢复Codex网络-0.2.1.exe')
lines = []
for name in names:
    path = out/name
    lines.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+name)
    if name.endswith('.zip'):
        with zipfile.ZipFile(path) as z:
            assert z.testzip() is None
    print(name, path.stat().st_size)
(out/'SHA256SUMS-0.2.1.txt').write_text('\n'.join(lines)+'\n', encoding='utf-8')
assert hashlib.sha256((out/'Codex配额管家-0.2.1.exe').read_bytes()).digest() == hashlib.sha256((dist/'Codex配额管家.exe').read_bytes()).digest()
print('DELIVERABLES_AND_ARCHIVE_INTEGRITY_OK')
