"""Group selected screenshots only when the game identity is explicit."""

from dataclasses import dataclass, field
import hashlib
from pathlib import Path

from matchlog.recognition import parse_game_key

MATCH_PAGES = {'summary', 'rank'}
MAX_IMAGES = 20
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


def image_digest(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError('单张截图须为可读取的文件，且不超过 64 MB')
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def check_image_dimensions(path):
    from PIL import Image

    with Image.open(path) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError('图片尺寸过大，请截取游戏画面后重试')


@dataclass
class ImportPage:
    path: Path
    digest: str
    result: dict

    @property
    def key(self):
        raw = self.result.get('game_key')
        return parse_game_key(raw) if isinstance(raw, str) else None


@dataclass
class ImportGroup:
    game_key: str | None
    target_id: str | None = None
    pages: list[ImportPage] = field(default_factory=list)

    def preview(self):
        return {'pages': [{'name': page.path.name, 'kind': page.result['kind']} for page in self.pages],
                'target_id': self.target_id}


def plan_import(pages, lookup_match, target_id=None, target_keys=()):
    groups = []
    seen = set()
    keyed = {}
    target = None
    if target_id:
        target = ImportGroup(None, target_id)
    for page in pages:
        if page.digest in seen:
            continue
        seen.add(page.digest)
        if page.result['kind'] not in MATCH_PAGES:
            if target:
                raise ValueError('补充指定对局时，请选择三人总结或经验值分数截图')
            groups.append(ImportGroup(None, pages=[page]))
            continue
        if target:
            if page.key and ((target_keys and page.key not in target_keys)
                             or (target.game_key and page.key != target.game_key)):
                raise ValueError('截图场次标识不一致，请分别导入')
            if any(p.result['kind'] == page.result['kind'] for p in target.pages):
                raise ValueError('一次只可给指定对局补一张总结和一张分数图')
            target.game_key = target.game_key or page.key
            target.pages.append(page)
        elif not page.key:
            groups.append(ImportGroup(None, pages=[page]))
        else:
            if page.key not in keyed:
                keyed[page.key] = ImportGroup(page.key, lookup_match(page.key))
                groups.append(keyed[page.key])
            group = keyed[page.key]
            if any(p.result['kind'] == page.result['kind'] for p in group.pages):
                raise ValueError('同一局选了多张同类截图，请保留最清晰的一张')
            group.pages.append(page)
    return [target] if target else groups
