"""Lossless archives: preserve every OCR pixel while avoiding large PNG duplicates."""
import hashlib
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from PIL import Image
from matchlog.media_paths import existing_media_path, media_dir, resolve_media_path


CHINA = timezone(timedelta(hours=8))


def capture_name(kind, stamp, suffix, serial=1, *, incident=False):
    counter = f'-{serial:02d}' if serial > 1 else ''
    marker = '-incident' if incident else ''
    return f'{kind}-{stamp.astimezone(CHINA):%y%m%d-%H%M%S}{counter}{marker}{suffix}'


def timestamp_path(folder, kind, timestamp=None, suffix='.webp'):
    folder = Path(folder)
    if folder.resolve() != folder.absolute():
        raise ValueError('截图路径不安全')
    stamp = datetime.now(CHINA) if timestamp is None else datetime.fromtimestamp(timestamp, CHINA)
    serial = 1
    while True:
        path = folder / capture_name(kind, stamp, suffix, serial)
        if not path.exists() and not path.is_symlink():
            return path
        serial += 1

def save_archive(image, path):
    path = Path(path).with_suffix('.webp')
    if path.resolve() != path.absolute():
        raise ValueError('截图路径不安全')
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix='.part', delete=False) as file:
        temporary = Path(file.name)
    try:
        image.convert('RGB').save(temporary, format='WEBP', lossless=True, method=3)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def cached_asset(store, category, digest, folder):
    row = store.db.execute('SELECT path FROM media_asset_paths WHERE category=? AND digest=?',
                           (category,digest)).fetchone()
    path = existing_media_path(store.root,row[0]) if row else None
    return path if path is not None and path.parent==folder else None


def remember_asset(store, category, digest, path):
    store.db.execute('INSERT OR REPLACE INTO media_asset_paths VALUES (?,?,?)',(category,digest,str(path)))


def archive_settlement(store, kind, raw, timestamp=None):
    """Store the same screenshot once while leaving match identity to the caller."""
    digest = hashlib.sha256(raw).hexdigest()
    if kind not in ('rank', 'summary'):
        raise ValueError('结算截图类型不正确')
    folder = media_dir(store.root, kind)
    with store.lock:
        target = cached_asset(store,kind,digest,folder)
        if target is not None:
            try:
                os.utime(target, None)
                return target, False
            except FileNotFoundError:
                pass
        target = resolve_media_path(store.root, timestamp_path(folder, kind, timestamp))
        with Image.open(io.BytesIO(raw)) as image:
            save_archive(image, target)
        remember_asset(store,kind,digest,target)
        store.db.commit()
        return target, True
