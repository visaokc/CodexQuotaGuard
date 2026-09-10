import importlib.metadata as metadata
from pathlib import Path


root = Path(__file__).resolve().parents[1]
assets = root/'assets'
assets.mkdir(exist_ok=True)
import sys
sys.path.insert(0, str(root))
from quota_guard.app_icon import icon_image
image = icon_image()
image.save(assets/'app.ico', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
image.save(assets/'app.png')
licenses = root/'licenses'
licenses.mkdir(exist_ok=True)
notices = ['Tailscale tsnet v1.102.3 | BSD-3-Clause | See licenses/tsnet for upstream and dependency notices.', 'Third-party software packaged with Codex Quota Guard 0.4.1', '',
           'Project implementation is original. Token Monitor and Cockpit Tools were read as references, not vendored.',
           'Python is bundled by PyInstaller. The WebView2 runtime is supplied by Microsoft on the host system. See corresponding license files.',
           'Microsoft WebView2 SDK 1.0.3856.49 | See licenses/Microsoft-WebView2-SDK-LICENSE.txt and NOTICE.txt.', '']
for name in ('cryptography', 'pywebview', 'pythonnet', 'clr_loader', 'proxy_tools', 'bottle', 'cffi', 'pycparser',
             'typing-extensions', 'packaging', 'pillow', 'pystray', 'six', 'pyinstaller'):
    dist = metadata.distribution(name)
    notices.append(f'{dist.metadata["Name"]} {dist.version} | {dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See license files"}')
    for item in dist.files or []:
        if any(word in item.name.lower() for word in ('license', 'copying', 'notice')) and item.suffix not in ('.py', '.pyc'):
            source = Path(dist.locate_file(item))
            if source.is_file():
                target = licenses/(name+'-'+str(item).replace('/', '_').replace('\\', '_'))
                target.write_bytes(source.read_bytes())
base = Path(__import__('sys').base_prefix)
for source in (base/'pkgs').glob('tk-*/info/licenses/tcl*/license.terms'):
    (licenses/(source.parent.name+'-'+source.name)).write_bytes(source.read_bytes())
for source in [base/'LICENSE.txt', base/'LICENSE_PYTHON.txt',
               base/'tcl'/'tcl8.6'/'license.terms', base/'tcl'/'tk8.6'/'license.terms',
               base/'Library'/'lib'/'tcl8.6'/'license.terms', base/'Library'/'lib'/'tk8.6'/'license.terms']:
    if source.exists():
        (licenses/(source.parent.name+'-'+source.name)).write_bytes(source.read_bytes())
frontend = root/'frontend'/'node_modules'
for name in ('vue', '@vue/shared', '@vue/reactivity', '@vue/runtime-core', '@vue/runtime-dom', '@vue/compiler-dom', '@vue/compiler-core'):
    folder = frontend/name
    package = folder/'package.json'
    if package.exists():
        import json
        details = json.loads(package.read_text(encoding='utf-8'))
        notices.append(f'{name} {details["version"]} | {details.get("license", "See license file")}')
        if (folder/'LICENSE').exists():
            (licenses/(name.replace('/', '_')+'-LICENSE.txt')).write_bytes((folder/'LICENSE').read_bytes())
(root/'THIRD_PARTY_NOTICES.txt').write_text('\n'.join(notices), encoding='utf-8')
print('ASSETS_AND_LICENSES_OK')
