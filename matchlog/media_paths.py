"""Resolve stored screenshot references within the managed data directory."""

from pathlib import Path


MEDIA_LAYOUT = {
    'draft': ('capture', 'draft'),
    'hud': ('capture', 'hud'),
    'lobby': ('capture', 'lobby'),
    'profiles': ('capture', 'profiles'),
    'rank': ('capture', 'rank'),
    'summary': ('capture', 'summary'),
    'restored': ('capture', 'restored'),
    'highlights': ('capture', 'highlights'),
    'hero_samples': ('sample', 'heroes'),
    'reference_samples': ('sample', 'references'),
}
LEGACY_LAYOUT = {
    'draft': (('capture', 'inbox'), ('inbox',)),
    'captures': (('capture', 'settlement'), ('captures',)),
    **{key: ((key,),) for key in ('hud', 'lobby', 'profiles', 'restored', 'highlights',
                                  'hero_samples', 'reference_samples')},
}
IMAGE_FOLDERS = frozenset((*MEDIA_LAYOUT.values(), *(parts for paths in LEGACY_LAYOUT.values() for parts in paths)))
IMAGE_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.bmp', '.webp'})
DEVICE_NAMES = frozenset({'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    f'{prefix}{index}' for prefix in ('COM', 'LPT') for index in '123456789¹²³'
})


def media_dir(root, kind):
    """Return the current directory for a media category."""
    return media_dirs(root, kind)[0]


def media_dirs(root, kind):
    """Return the current location followed by supported historical locations."""
    root = Path(root).resolve()
    kind = 'draft' if kind == 'inbox' else kind
    paths = LEGACY_LAYOUT.get(kind, ())
    if kind in MEDIA_LAYOUT:
        paths = (MEDIA_LAYOUT[kind], *paths)
    if not paths:
        raise KeyError(kind)
    return tuple(root.joinpath(*parts) for parts in paths)


def resolve_media_path(root, value):
    """Return a safe absolute path, even when the image has been cleaned up."""
    try:
        root = Path(root).absolute()
        resolved_root = root.resolve()
        path = Path(value)
        if not path.is_absolute():
            raise ValueError
        base = root if path.is_relative_to(root) else resolved_root
        relative = path.relative_to(base)
        folders = relative.parts[:-1]
        managed = folders[:1] in IMAGE_FOLDERS or folders[:2] in IMAGE_FOLDERS
        if (not managed
                or path.suffix.lower() not in IMAGE_SUFFIXES
                or any(part == '..' or part.endswith(('.', ' '))
                       or part.split('.', 1)[0].rstrip().upper() in DEVICE_NAMES
                       or any(char in part for char in '<>:"|?*\x00')
                       for part in relative.parts)):
            raise ValueError
        resolved = path.resolve()
        if resolved != resolved_root / relative:
            raise ValueError
        return resolved
    except (TypeError, ValueError, OSError, RuntimeError) as error:
        raise ValueError('截图路径不安全') from error


def existing_media_path(root, value):
    """Return a readable candidate for optional evidence and missing-file fallbacks."""
    try:
        path = resolve_media_path(root, value)
        return path if path.is_file() else None
    except (ValueError, OSError):
        return None
