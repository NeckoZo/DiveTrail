"""Validate rank divisions against visible Roman-numeral badges."""

from functools import lru_cache
from pathlib import Path


TIERS = {'新秀': 'rookie', '青铜': 'bronze', '白银': 'silver', '黄金': 'gold',
         '白金': 'platinum', '钻石': 'diamond'}
ROMAN = {1: 'I', 2: 'II', 3: 'III', 4: 'IV'}
ASSETS = Path(__file__).resolve().parents[1] / 'ui' / 'assets' / 'ranks'


@lru_cache(maxsize=512)
def _template(tier, division, width):
    import numpy as np
    from PIL import Image

    with Image.open(ASSETS / f'{tier}-{division}.png') as source:
        icon = source.convert('RGBA')
    icon = icon.crop(icon.getchannel('A').getbbox())
    height = round(width * icon.height / icon.width)
    pixels = np.asarray(icon.resize((width, height), Image.Resampling.LANCZOS))
    mask = (pixels[..., 3] >= 220).astype(np.float32)
    # Adjacent divisions share the shield, but their Roman numerals differ.
    mask[:round(height * .76)] *= .25
    return np.ascontiguousarray(pixels[..., :3]), mask


def _division(image, bounds, tier, *, reference_width=None):
    import cv2
    import numpy as np

    box = tuple(round(value * image.size[i % 2]) for i, value in enumerate(bounds))
    if box[0] < 0 or box[1] < 0 or box[2] > image.width or box[3] > image.height:
        return None
    crop = np.asarray(image.crop(box).convert('RGB'))
    search = cv2.resize(crop, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA)
    widths = sorted({round(scale * (reference_width or image.width) * .5) for scale in np.linspace(.036, .066, 18)})
    results = []
    for division in ROMAN:
        scores = []
        for width in widths:
            template, mask = _template(tier, division, width)
            if template.shape[0] > search.shape[0] or template.shape[1] > search.shape[1]:
                continue
            comparison = cv2.matchTemplate(search, template, cv2.TM_SQDIFF_NORMED, mask=mask)
            finite = comparison[np.isfinite(comparison)]
            if finite.size:
                scores.append(float(finite.min()))
        if scores:
            results.append((min(scores), division))
    results.sort()
    if len(results) >= 2 and results[0][0] <= .35 and results[1][0] - results[0][0] >= .10:
        return results[0][1]
    return None


def confirm(image, label, rank):
    """Resolve a doubtful numeral only when two adjacent badge templates agree."""
    from matchlog.rp_progress import rank_total

    if rank.get('rank_anchor_status') != 'ambiguous' or image.width < 800 or not label:
        return rank
    prefix = (rank.get('rank') or '').split(' ', 1)[0]
    if prefix not in TIERS or not (rank.get('next_rank') or '').startswith(prefix):
        return rank
    x, y, right, bottom = (label[field] for field in ('x', 'y', 'right', 'bottom'))
    try:
        before = _division(image, (x - .107, y - .085, x - .006, bottom + .076), TIERS[prefix])
        after = _division(image, (right + .002, y - .065, right + .135, bottom + .09), TIERS[prefix])
    except OSError:
        return rank
    if before is None or after != before - 1:
        return rank
    candidate = {**rank, 'rank': f'{prefix} {ROMAN[before]}', 'next_rank': f'{prefix} {ROMAN[after]}',
                 'rank_anchor_status': 'badge'}
    return candidate if rank_total(candidate) is not None else rank


def lobby_badge_rank(crop, rank):
    """Return an independently matched division on the left side of the lobby card."""
    tier = str(rank or '').split(' ', 1)[0].replace('铂金', '白金')
    if tier not in TIERS or crop.width < 160:
        return None
    try:
        division = _division(crop, (0, 0, .275, 1), TIERS[tier], reference_width=crop.width * 5)
    except OSError:
        division = None
    if division is None:
        division = _lobby_numeral(crop)
    return f'{tier} {ROMAN[division]}' if division else None


def _lobby_numeral(card):
    """Count aligned white I glyphs when scaled badge artwork loses template margin."""
    import cv2
    import numpy as np

    x, y = card.size
    glyph = card.crop((round(x*.105), round(y*.64), round(x*.21), round(y*.87)))
    pixels = np.asarray(glyph.convert('RGB'))
    white = ((pixels.min(axis=2)>145) & (np.ptp(pixels,axis=2)<65)).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(white)
    strokes = sorted(tuple(map(int,row)) for row in stats[1:] if row[3]>=glyph.height*.40)
    if not 1<=len(strokes)<=3:
        return None
    if any(not .18<=w/h<=.74 or not .42<=area/(w*h)<=.95 for _,_,w,h,area in strokes):
        return None
    start, top, _, height, _ = strokes[0]
    if any(abs(line_y-top)>2 or abs(line_h-height)>2 for _,line_y,_,line_h,_ in strokes):
        return None
    if any(b[0]-a[0]-a[2] > height*.45 for a,b in zip(strokes,strokes[1:])):
        return None
    center = (round(x*.105)+(start+strokes[-1][0]+strokes[-1][2])/2)/x
    return len(strokes) if .143<=center<=.177 else None
