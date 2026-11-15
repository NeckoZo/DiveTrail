"""Screenshot retention, restricted to known folders beneath the local store."""

import time

from matchlog.media_paths import IMAGE_SUFFIXES, media_dirs, resolve_media_path


FOLDERS = ('draft', 'hud', 'lobby', 'profiles', 'rank', 'summary', 'captures', 'restored')
DAY = 86400


def clean_images(store, mode='auto', now=None):
    if mode not in ('auto', 'expired', 'temporary', 'all'):
        raise ValueError('清理选项不正确')
    now = time.time() if now is None else now
    root = store.root.resolve()
    result = {'files': 0, 'bytes': 0}

    def remove(path):
        try:
            length = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        result['files'] += 1
        result['bytes'] += length
        return True

    with store.lock:
        config = store.settings()
        if mode in ('auto', 'expired') and config['highlight_days']:
            cutoff = now - config['highlight_days'] * DAY
            rows = store.db.execute("""
                SELECT h.match_id,h.path FROM match_highlights h
                JOIN matches m ON m.id=h.match_id
                WHERE COALESCE(CAST(strftime('%s',json_extract(m.payload,'$.time')) AS INTEGER),m.created)<=?
                """, (cutoff,)).fetchall()
            for ident, value in rows:
                try:
                    path = resolve_media_path(root, value)
                except ValueError:
                    continue
                # Imported images can share one file. Let ordinary retention
                # handle the restored file after dropping the highlight reference.
                restored = media_dirs(root, 'restored')
                highlights = media_dirs(root, 'highlights')
                if path.parent in restored or (path.parent in highlights and remove(path)):
                    store.db.execute('DELETE FROM match_highlights WHERE match_id=?', (ident,))
        if mode != 'auto' or config['auto_cleanup']:
            _clean_screenshots(store, root, mode, now, config, remove)
        store.db.commit()
    return result


def _clean_screenshots(store, root, mode, now, config, remove):
    expired = mode in ('auto', 'expired')
    protected = set()
    for (value,) in store.db.execute('SELECT path FROM match_highlights'):
        try:
            protected.add(resolve_media_path(root, value))
        except ValueError:
            continue
    review = {}
    review_jobs = []
    for ident, value, status, at in store.db.execute('SELECT id,path,status,created FROM capture_jobs'):
        try:
            path = resolve_media_path(root, value)
        except ValueError:
            continue
        if status in ('pending', 'processing', 'deferred'):
            protected.add(path)
        elif status in ('review', 'failed'):
            review[path] = max(at, review.get(path, at))
            review_jobs.append((ident, path))
    for name in FOLDERS:
        if mode == 'temporary' and name != 'draft':
            continue
        for path in (path for folder in media_dirs(root, name)
                     if folder.is_dir() and folder.resolve() == folder for path in folder.iterdir()):
            if path.suffix.lower() not in IMAGE_SUFFIXES or path in protected:
                continue
            if path.is_symlink() or not path.is_file() or path.resolve() != path:
                continue
            if path in review:
                if not expired or now - review[path] < config['cleanup_review_days'] * DAY:
                    continue
            elif expired:
                days = config['cleanup_temporary_days' if name == 'draft' else 'cleanup_original_days']
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                if now - modified < days * DAY:
                    continue
            remove(path)
    for ident, path in review_jobs:
        if not path.is_file():
            store.db.execute("UPDATE capture_jobs SET status='dismissed',error='截图已清理' WHERE id=?", (ident,))
