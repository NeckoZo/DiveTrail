"""Small header templates keep the OCR model out of ordinary gameplay checks."""
from pathlib import Path


# Three separated portraits in the lower row of the legend selection grid.
SELECTION_SLOTS = (('bloodhound', .002, .115), ('lifeline', .40, .55), ('wattson', .77, .91))
SELECTION_BAND = (.71, .985)


class ScreenScout:
    def __init__(self):
        import cv2
        cv2.setNumThreads(1);cv2.ocl.setUseOpenCL(False)
        self._cv=cv2
        folder=Path(__file__).resolve().parents[1]/'ui/assets/detection'
        self._templates={}
        for name in ['summary','xp','pass','rank_label','hud','lobby_nav','lobby_mode','range_label','range_shots','range_headshots','ranked_intro','season_rank','season_live','season_summary','profile_data','profile_progress']:
            path=folder/(name.replace('_','-')+'.png')
            if not path.is_file():continue
            source=cv2.imread(str(path),cv2.IMREAD_GRAYSCALE)
            if source is None:
                continue
            self._templates[name]=[cv2.resize(source,None,fx=s,fy=s) for s in [.8,.9,1,1.1,1.2]]
        self._selection=[]
        for name,_,_ in SELECTION_SLOTS:
            path=folder.parent/'heroes/references'/name/'roster.png'
            source=cv2.imread(str(path),cv2.IMREAD_GRAYSCALE) if path.is_file() else None
            if source is None:
                self._selection.clear()
                break
            scale=640/1961
            self._selection.append([cv2.resize(source,None,fx=scale*s,fy=scale*s,interpolation=cv2.INTER_AREA)
                                    for s in (.86,1,1.14)])

    @property
    def requires_text(self):
        return not {'summary','xp','hud','lobby_nav'} <= self._templates.keys()

    def is_selection(self, patches, width):
        """Require distinct portraits aligned on one row; a single HUD portrait cannot qualify."""
        import numpy as np
        if len(self._selection)!=len(SELECTION_SLOTS) or len(patches)!=len(SELECTION_SLOTS):return False
        rows=[]
        for image,templates in zip(patches,self._selection):
            size=tuple(max(1,round(edge*640/width)) for edge in image.size)
            gray=np.asarray(image.convert('L').resize(size))
            best=(0,0)
            for template in templates:
                if all(outer>=inner for outer,inner in zip(gray.shape,template.shape)):
                    response=self._cv.matchTemplate(gray,template,self._cv.TM_CCOEFF_NORMED)
                    _,score,_,position=self._cv.minMaxLoc(response)
                    if score>best[0]:best=(score,position[1]+template.shape[0]//2)
            if best[0]<.82:return False
            rows.append(best[1])
        return max(rows)-min(rows)<=8

    def hint(self, image):
        w,h=image.size
        return self.hint_regions(image.crop((0,0,w,round(h*.15))),image.crop((0,round(h*.60),round(w*.25),round(h*.80))),image.size)

    def _score_region(self, name, region):
        best=0
        cv=self._cv
        for template in self._templates.get(name,[]):
            if template.shape[0]<=region.shape[0] and template.shape[1]<=region.shape[1]:
                best=max(best,float(cv.minMaxLoc(cv.matchTemplate(region,template,cv.TM_CCOEFF_NORMED))[1]))
        return best

    def has_rank_card(self, image):
        import numpy as np
        w,h=image.size
        top=image.crop((0,round(h*.051),w,round(h*.141)))
        gray=np.asarray(top.convert('L').resize((1280,max(1,round(top.height*1280/w)))))
        if not (self._score_region('summary',gray[:,round(.27*1280):round(.58*1280)])>.78
                and self._score_region('xp',gray[:,round(.43*1280):round(.71*1280)])>.78):
            return False
        card=image.crop((round(w*.39),round(h*.16),round(w*.60),round(h*.39))).convert('L')
        gray=np.asarray(card.resize(tuple(round(edge*1280/w) for edge in card.size)))
        return self._score_region('rank_label',gray)>.85

    def hint_regions(self, top, bottom, size):
        import numpy as np
        if not self._templates:
            return 'other'
        cv=self._cv
        # Normalize only a small top strip, never run recognition on the full playfield.
        w,h=size
        top=top.resize((1280,max(1,round(top.height*1280/w))))
        gray=cv.cvtColor(np.asarray(top),cv.COLOR_RGB2GRAY)
        def score(name,left,right):return self._score_region(name,gray[:,round(left*1280):round(right*1280)])
        if score('profile_data',.30,.50)>.82 and score('profile_progress',.50,.68)>.82:return 'profile'
        if score('ranked_intro',0,.28)>.82:return 'intro'
        if score('range_label',.85,1)>.90 and max(score('range_shots',.26,.73),score('range_headshots',.26,.73))>.87:
            return 'training'
        found=sum(score(name,.29,.71)>.78 for name in ['summary','xp','pass'])
        if found>=2:
            return 'settlement'
        hud=score('hud',.74,1)>.78
        if sum(score(name,.28,.71)>.80 for name in ('season_rank','season_live','season_summary'))>=2:
            # A selected top-level Live tab also identifies spectating if the
            # watched player has no squad count on screen.
            tabs=gray[round(gray.shape[0]*.35):round(gray.shape[0]*.62)]
            rank,live,summary=(tabs[:,x-20:x+20].mean(axis=1).max() for x in (505,640,768))
            adjacent=max(rank,summary)
            selected_live=live>190 and adjacent<190 and live>adjacent+24
            return 'spectator' if hud or selected_live else 'postmatch'
        if hud:
            return 'hud'
        if score('lobby_nav',.20,.35)>.78:
            if bottom is None:return 'lobby_candidate'
            bottom=bottom.resize((320,max(1,round(bottom.height*1280/w))))
            if self._score_region('lobby_mode',cv.cvtColor(np.asarray(bottom),cv.COLOR_RGB2GRAY))>.78 or score('lobby_nav',.20,.35)>.86:
                return 'lobby'
        return 'other'
