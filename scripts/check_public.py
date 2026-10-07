"""Check tracked public files without printing potentially private matching content."""
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

root = Path(__file__).resolve().parents[1]
arguments = ['git','ls-files','--cached','-z']
if '--include-untracked' in sys.argv:
    arguments += ['--others','--exclude-standard']
tracked = sorted(set(subprocess.check_output(arguments, cwd=root).decode().split('\0')))
errors = []
for name in filter(None, tracked):
    path = root / name
    if not path.is_file():
        continue  # A staged/public tree is checked by CI after checkout.
    parts = Path(name).parts
    if any(p in {'.venv','node_modules','__pycache__','.env'} for p in parts) or path.suffix in {'.db','.sqlite','.sqlite3','.pem','.key','.log'}:
        errors.append(name + ': private/generated file is tracked')
    if path.suffix.lower() not in {'.md','.py','.sh','.json','.yml','.yaml','.toml','.txt','.js','.mjs','.html','.css'}:
        continue
    text = path.read_text()
    # Concrete private filesystem paths and tailnet names; documented placeholders are allowed.
    if re.search(r'/home/(?!example(?:/|\b)|user(?:/|\b)|x(?:/|\b))[A-Za-z0-9_.-]+/|/mnt/c/[Uu]sers/(?!Example/|<)[^/\s]+/|[A-Z]:\\Users\\[^\\\s]+', text):
        errors.append(name + ': private absolute path')
    for host in re.findall(r'\b[a-zA-Z0-9.-]+\.ts\.net\b', text):
        if host != 'example.ts.net' and not host.endswith('.example.ts.net'):
            errors.append(name + ': private tailnet hostname')
            break
    if path.suffix == '.md':
        for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
            target = target.strip('<>').split(' "',1)[0]
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            if not resolved.is_relative_to(root) or not resolved.exists():
                errors.append(name + ': broken or nonportable local documentation link')
if errors:
    print('\n'.join(sorted(set(errors))))
    sys.exit(1)
print('Tracked file privacy patterns and local documentation links passed')
