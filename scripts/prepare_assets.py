import importlib.metadata as metadata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parents[1]
assets = root/'assets'
assets.mkdir(exist_ok=True)
image = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((8, 8, 248, 248), radius=66, fill='#69d9bd')
font = ImageFont.truetype(r'C:\Windows\Fonts\segoeuib.ttf', 175)
draw.text((128, 113), 'C', font=font, fill='#142c29', anchor='mm')
image.save(assets/'app.ico', sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
licenses = root/'licenses'
licenses.mkdir(exist_ok=True)
notices = ['Syncthing v2.1.3-cqg1 | MPL-2.0 | Modified proxy compatibility build; see syncthing-source-notices/BUILD.md and modified-source.', 'Third-party software packaged with Codex Quota Guard 0.2.6', '',
           'Project implementation is original. Token Monitor and Cockpit Tools were read as references, not vendored.',
           'Python and Tcl/Tk are bundled by PyInstaller. See corresponding license files.', '']
for name in ('aiortc', 'aioice', 'av', 'websockets', 'cryptography', 'customtkinter', 'darkdetect',
             'cffi', 'pycparser', 'google-crc32c', 'pyee', 'pylibsrtp', 'pyopenssl', 'dnspython',
             'ifaddr', 'typing-extensions', 'packaging', 'pillow', 'pystray', 'six', 'pyinstaller'):
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
(root/'THIRD_PARTY_NOTICES.txt').write_text('\n'.join(notices), encoding='utf-8')
print('ASSETS_AND_LICENSES_OK')
