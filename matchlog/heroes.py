"""Conservative matching against labelled HUD portraits; unknown faces stay unknown."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import threading

HERO_BOXES=[(.035,.748,.065,.795),(.034,.804,.067,.854),(.045,.897,.085,.95)]
NAME_BOXES=[(.071,.749,.205,.780),(.071,.804,.225,.839),(.089,.895,.215,.928)]
LABELS={'vantage':'万蒂奇','loba':'罗芭','pathfinder':'探路者','crypto':'密客',
        'bangalore':'班加罗尔','ash':'艾许','alter':'变幻','caustic':'侵蚀','wraith':'恶灵',
        'lifeline':'命脉','seer':'希尔','conduit':'导管','mad-maggie':'疯玛吉',
        'valkyrie':'瓦尔基里','newcastle':'纽卡斯尔','wattson':'沃特森',
        'octane':'动力小子','catalyst':'催化剂','mirage':'幻象','revenant':'亡灵','axle':'艾瑟儿',
        'fuse':'暴雷','ballistic':'弹道','horizon':'地平线','bloodhound':'寻血猎犬',
        'sparrow':'雀影','gibraltar':'直布罗陀','rampart':'兰伯特'}

def canonical_hero(value):return {'Axle':'艾瑟儿','Alex':'艾瑟儿','艾瑟兒':'艾瑟儿'}.get(value,value)


class HeroSamples:
    """Only face crops are retained; a human correction supplies the missing label."""
    _lock=threading.RLock()
    def __init__(self, folder):
        self.folder=Path(folder)
        self.index=self.folder/'index.json'
        self.lock=self._lock

    def entries(self):
        try:return json.loads(self.index.read_text(encoding='utf-8'))
        except (FileNotFoundError,ValueError):return {}

    def _write(self, entries):
        self.folder.mkdir(parents=True,exist_ok=True)
        temp=self.index.with_suffix('.tmp')
        temp.write_text(json.dumps(entries,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(self.index)

    def remember(self, portrait):
        import cv2
        import numpy as np
        crop=portrait.convert('RGB').resize((64,64))
        gray=cv2.cvtColor(np.asarray(crop),cv2.COLOR_RGB2GRAY)
        # Blank, extremely blurred, or tiny crops cannot become useful references.
        if min(portrait.size)<24 or gray.std()<12 or cv2.Laplacian(gray,cv2.CV_64F).var()<18:return None
        small=cv2.resize(gray,(16,16)).astype(float)
        small=(small-small.mean())/max(small.std(),1)
        signature=small.round(2).ravel()
        with self.lock:
            entries=self.entries()
            for ident,row in entries.items():
                if row.get('signature') and np.mean((signature-np.asarray(row['signature']))**2)<.10:
                    return ident
            if len(entries)>=200:return None
            ident=hashlib.sha256(crop.tobytes()).hexdigest()[:20]
            self.folder.mkdir(parents=True,exist_ok=True)
            crop.save(self.folder/(ident+'.png'))
            entries[ident]={'hero':None,'created_at':datetime.now(timezone.utc).isoformat(),
                            'signature':signature.tolist()}
            self._write(entries)
            return ident

    def confirm(self, ident, hero):
        hero=canonical_hero(str(hero or '').strip()[:40])
        with self.lock:
            entries=self.entries()
            if not hero or ident not in entries:return False
            entries[ident]['hero']=hero
            entries[ident]['confirmed_at']=datetime.now(timezone.utc).isoformat()
            self._write(entries)
        return True


class HeroRecognizer:
    def __init__(self, samples=None):
        import cv2
        self.cv=cv2
        self.samples=HeroSamples(samples) if samples else None
        self._sample_version=None
        self._load()

    def _load(self):
        cv2=self.cv
        self.references={}
        folder=Path(__file__).resolve().parents[1]/'ui/assets/heroes/references'
        try:
            variants=json.loads((folder/'sources.json').read_text(encoding='utf-8')).get('variants',[])
        except (OSError, ValueError):
            variants=[]
        excluded={row['file'] for row in variants if row.get('exclude')}
        for path in folder.glob('*/*.png'):
            key=path.parent.name
            if key not in LABELS or path.relative_to(folder).as_posix() in excluded:continue
            image=cv2.imread(str(path),cv2.IMREAD_GRAYSCALE)
            if image is not None:self.references.setdefault(LABELS[key],[]).append(cv2.resize(image,(40,40))[6:35,8:33])
        if self.samples:
            for ident,row in self.samples.entries().items():
                if row.get('hero') and len(ident)==20 and all(c in '0123456789abcdef' for c in ident):
                    image=cv2.imread(str(self.samples.folder/(ident+'.png')),cv2.IMREAD_GRAYSCALE)
                    if image is not None:self.references.setdefault(canonical_hero(row['hero']),[]).append(cv2.resize(image,(40,40))[6:35,8:33])
            self._sample_version=self.samples.index.stat().st_mtime_ns if self.samples.index.exists() else None

    def identify(self, portrait):
        import numpy as np
        cv=self.cv
        if self.samples:
            version=self.samples.index.stat().st_mtime_ns if self.samples.index.exists() else None
            if version!=self._sample_version:self._load()
        gray=cv.cvtColor(np.asarray(portrait.convert('RGB')),cv.COLOR_RGB2GRAY)
        gray=cv.resize(gray,(40,40))[4:37,6:35]
        if gray.std()<8:return None
        # Compare each hero's best variant, then require a gap to a DIFFERENT hero.
        scores=sorted(((max(float(cv.matchTemplate(gray,ref,cv.TM_CCOEFF_NORMED).max()) for ref in refs),name)
                       for name,refs in self.references.items()),reverse=True)
        if not scores or scores[0][0]<.82 or (len(scores)>1 and scores[0][0]-scores[1][0]<.12):
            return None
        return scores[0][1]

    def observe(self, portrait):
        hero=self.identify(portrait)
        return {'hero':hero,'hero_source':'hud_portrait' if hero else None,
                'hero_sample':self.samples.remember(portrait) if not hero and self.samples else None}


def fractional_crop(image, box):
    return image.crop(tuple(round(value*image.size[i%2]) for i,value in enumerate(box)))
