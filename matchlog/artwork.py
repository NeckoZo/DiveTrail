"""Read local and bundled artwork, loading image bytes only on request."""

import base64
import json
from pathlib import Path


ARTWORK_MIME = {'.webp': 'image/webp', '.png': 'image/png'}
GALLERY_FIELDS = ('id', 'hero', 'name', 'rarity', 'size', 'bounds', 'source', 'attribution')


def read_artwork_index(root):
    try:
        index = json.loads((root / 'index.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    rows = index.get('images') if isinstance(index, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and all(
        isinstance(row.get(field), str) and row[field].strip()
        for field in ('id', 'hero', 'name', 'file')
    )]


def artwork_path(root, row):
    relative = Path(row['file'])
    if (relative.anchor or relative.suffix.lower() not in ARTWORK_MIME
            or any(part.startswith('.') or ':' in part for part in relative.parts)):
        return None
    try:
        path = (root / relative).resolve()
        if path.is_relative_to(root) and path.suffix.lower() in ARTWORK_MIME and path.is_file():
            return path
    except (OSError, RuntimeError, ValueError):
        pass
    return None


class Artwork:
    def __init__(self, root, bundled_root=None):
        self.root = Path(root).resolve()
        self._roots = (self.root,)
        if bundled_root is not None:
            bundled_root = Path(bundled_root).resolve()
            if bundled_root != self.root:
                self._roots += (bundled_root,)
        self._catalogues = {}

    def _indexed_rows(self, root):
        try:
            stamp = (root / 'index.json').stat().st_mtime_ns
        except OSError:
            stamp = None
        previous, rows = self._catalogues.get(root, (None, []))
        if stamp != previous:
            rows = read_artwork_index(root)
            self._catalogues[root] = stamp, rows
        return rows

    def rows(self):
        rows = {}
        for root in self._roots:
            for row in self._indexed_rows(root):
                if row['id'] not in rows and artwork_path(root, row):
                    rows[row['id']] = row
        return list(rows.values())

    def gallery(self):
        return [{field: row[field] for field in GALLERY_FIELDS if field in row} for row in self.rows()]

    def image(self, ident):
        ident = str(ident)
        for root in self._roots:
            for row in self._indexed_rows(root):
                if row['id'] != ident or (path := artwork_path(root, row)) is None:
                    continue
                try:
                    encoded = base64.b64encode(path.read_bytes()).decode('ascii')
                except OSError:
                    continue
                mime = ARTWORK_MIME[path.suffix.lower()]
                return f'data:{mime};base64,{encoded}'
        raise ValueError('素材不存在')
