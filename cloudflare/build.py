"""Package only public dashboard assets; never the book, credentials or .env."""
from pathlib import Path
import hashlib
import shutil

root = Path(__file__).resolve().parents[1]
source = root / 'src/cst/dashboard'
out = root / 'cloudflare/public'
(out / 'static').mkdir(parents=True, exist_ok=True)
for name in ('styles.css', 'app.js'):
    shutil.copyfile(source / name, out / 'static' / name)
html = (source / 'index.html').read_text()
html = html.replace('<head>', '<head><script>window.CST_REMOTE = true;</script>')
for name in ('styles.css', 'app.js'):
    digest = hashlib.sha256((source / name).read_bytes()).hexdigest()[:16]
    html = html.replace('/static/' + name, '/static/' + name + '?v=' + digest)
(out / 'index.html').write_text(html)
