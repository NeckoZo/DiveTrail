"""Local OCR. Screen values are evidence; missing values stay None."""
from pathlib import Path
import logging
import os
import re
import threading
import time
import unicodedata

os.environ.setdefault('OMP_NUM_THREADS', '1')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
HUD_FIELDS=('kills','assists','participation','damage')
RANK_PAGE_REGIONS=((.27,0,.73,.44), (0,.44,1,.79), (0,.90,.4,1))
SCOUT_TEXT_WINDOWS=((0,.29),(.25,.75),(.70,1))


def scout_text_scene(tokens):
    def visible(left,right,top,bottom,confidence=.90):
        return {''.join(unicodedata.normalize('NFKC',t['text']).split()).strip(':')
                for t in tokens if t['score']>=confidence and left<=t['x']<right and top<=t['y']<bottom}

    top=visible(.20,.85,0,.078)
    second=visible(.26,.74,.05,.146)
    right=visible(.70,1,0,.13)
    if {'靶场','射击场','靶場','射擊場'} & right or is_training_text(' '.join(top | second | right)):
        return 'training'
    if '数据' in top and top & {'账户进度','账号进度'}:
        return 'profile'
    if len(second & {'死亡回放','总结','经验值','通行证'})>=2:
        return 'settlement'
    hud=any(label in text for text in right for label in ('剩余小队','小队数量','剩餘小隊','小隊數量'))
    if {'排位','实况','总结'} <= top:
        return 'spectator' if hud else 'postmatch'
    if any('观战' in text for text in top | right):
        return 'spectator'
    if '游玩' in top and len(top & {'挑战','通行证','传奇储物柜','商店'})>=2:
        return 'lobby'
    # The opening screen can merge its triangular emblem into the title string.
    opening=visible(0,.25,.018,.146,.86)
    if not hud and any(re.fullmatch(r'[Λ△▲A]?(?:排位赛|排位賽)',label) for label in opening):
        return 'intro'
    return 'hud' if hud else 'other'

def parse_game_key(text):
    text=unicodedata.normalize('NFKC',text).lower().replace(' ','')
    found=re.search(r'\b(\d{1,2}:\d{5,9}:\d{5,9}:[a-f0-9]{8,16})\b',text)
    return found[1] if found else None

def game_key_crop(image):
    w,h=image.size
    return image.crop((0,round(h*.963),round(w*.32),h))

def game_key_from_tokens(tokens):
    return parse_game_key('\n'.join(t['text'] for t in tokens if t['y']>.90 and t['x']<.4 and t.get('score',1)>=.90))


def hud_crop(image):
    w,h=image.size
    return image.crop((round(w*.75),round(h*.065),w,round(h*.125)))

def hud_rank_crop(image):
    w,h=image.size
    return image.crop((round(w*.94),round(h*.073),w,round(h*.118)))

def parse_live_rp(text):
    text=unicodedata.normalize('NFKC',text).replace('−','-').replace('＋','+')
    found=re.search(r'([+\-]\s*\d{1,4})\s*(?:排位|RP)',text,re.I)
    return int(found[1].replace(' ','')) if found else None


def lobby_crop(image):
    w,h=image.size
    return image.crop((round(w*.022),round(h*.608),round(w*.222),round(h*.704)))


LOBBY_NAME_BOX=(.119,.116,.272,.155)
LOBBY_NAME_SEARCH_BOX=(.09,.102,.32,.180)
LOBBY_UI_LABELS={'准备','准备就绪','未准备','游玩','关于','未上榜','大厅','排位赛','匹配中','寻找对局','取消','开始游戏'}


def squad_crop(image):
    w,h=image.size
    return image.crop((round(w*.025),round(h*.74),round(w*.225),round(h*.965)))


def nickname(value):
    value = unicodedata.normalize('NFKC', value)
    value = re.sub(r'^\s*\[[^\]]+\]\s*', '', value)
    key=re.sub(r'\s+', '', value).casefold()
    return key


def display_name(value):
    name = re.sub(r'^\s*\[[^\]]+\]\s*', '', value).strip()
    return name


def lobby_identity_name(tokens, *, min_score=.88):
    """Accept one high-confidence player-name line in the lobby's own-name strip."""
    if not tokens or len(tokens)>4:
        return None
    centers=[(t['y']+t['bottom'])/2 for t in tokens]
    if max(centers)-min(centers)>.25:
        return None
    ordered=sorted(tokens,key=lambda t:t['x'])
    first=unicodedata.normalize('NFKC',ordered[0]['text']).strip()
    if re.fullmatch(r'\[[^\]]+\]',first):
        ordered=ordered[1:]
    if not ordered or any(t['score']<min_score for t in ordered):
        return None
    text=unicodedata.normalize('NFKC',' '.join(t['text'] for t in ordered))
    name=display_name(text)
    if nickname(name) in LOBBY_UI_LABELS or not 1<=len(name)<=64 or not any(c.isalpha() for c in name):
        return None
    if any(not (c.isalnum() or unicodedata.category(c).startswith('M') or c in " -_.'·") for c in name):
        return None
    return name


def integer(value):
    match = re.fullmatch(r'\s*([+\-]?\d[\d,，]*)\s*', value)
    return int(match[1].replace(',', '').replace('，', '')) if match else None


def image_tokens(result, size, offset=(0, 0)):
    w,h=size
    left,top=offset
    tokens=[]
    for box,text,score in zip(result.boxes if result.txts is not None else [],result.txts or [],result.scores or []):
        xs=[float(p[0])+left for p in box]
        ys=[float(p[1])+top for p in box]
        tokens.append({'text':text,'score':float(score),'x':min(xs)/w,'y':min(ys)/h,
                       'right':max(xs)/w,'bottom':max(ys)/h})
    return tokens


def rank_page_regions(image):
    """Pack the tabs/card, breakdown rows and footer for one detector pass."""
    from PIL import Image
    w,h=image.size
    boxes=[tuple(round(edge*image.size[i%2]) for i,edge in enumerate(box)) for box in RANK_PAGE_REGIONS]
    footer_x=boxes[0][2]-boxes[0][0]+24
    footer_y=round(h*.22)
    breakdown_y=boxes[0][3]+16
    positions=((0,0),(0,breakdown_y),(footer_x,footer_y))
    canvas=Image.new('RGB',(w,breakdown_y+boxes[1][3]-boxes[1][1]),'black')
    for box,position in zip(boxes,positions):
        canvas.paste(image.crop(box),position)
    return canvas,list(zip(boxes,positions))


def rank_page_tokens(result, size, regions):
    w,h=size
    tokens=[]
    for box,text,score in zip(result.boxes if result.txts is not None else [],result.txts or [],result.scores or []):
        xs=[float(p[0]) for p in box];ys=[float(p[1]) for p in box]
        for (left,top,right,bottom),(x,y) in regions:
            if x<=sum(xs)/4<x+right-left and y<=sum(ys)/4<y+bottom-top:
                tokens.append({'text':text,'score':float(score),'x':max(left,left+min(xs)-x)/w,
                               'y':max(top,top+min(ys)-y)/h,'right':min(right,left+max(xs)-x)/w,
                               'bottom':min(bottom,top+max(ys)-y)/h})
                break
    return tokens


class Recognizer:
    def __init__(self, hero_samples=None):
        self._engine = None
        self._scout = None
        self._next_text_scout = 0
        self._heroes = None
        self._hud_marker = None
        self._damage_marker = None
        self._hero_samples = hero_samples
        self._lock = threading.Lock()
        self.last_read_route = None
        self.last_read_pixels = None

    def _initialize(self):
        if self._engine is None:
            import cv2
            cv2.setNumThreads(1);cv2.ocl.setUseOpenCL(False)
            from rapidocr import RapidOCR
            self._engine = RapidOCR(params={'EngineConfig.onnxruntime.intra_op_num_threads': 1, 'EngineConfig.onnxruntime.inter_op_num_threads': 1})

    def _detect_text(self, image, *, upright=False):
        """RapidOCR retains the previous call's mode when flags are omitted."""
        return self._engine(image,use_det=True,use_cls=not upright,use_rec=True)

    def screen_hint_regions(self, top, bottom, size):
        if self._scout is None:
            from matchlog.screen_scout import ScreenScout
            self._scout=ScreenScout()
        hint=self._scout.hint_regions(top,bottom,size)
        if hint!='other' or not self._scout.requires_text:
            return hint
        now=time.monotonic()
        if now<self._next_text_scout or not self._lock.acquire(blocking=False):
            return None
        try:
            self._next_text_scout=now+1.5
            return self._text_scout(top,size)
        finally:
            self._lock.release()

    def _text_scout(self, top, size):
        """Pack the three header strips into a short upright detector input."""
        import numpy as np
        from PIL import Image

        top=top.copy()
        top.thumbnail((2000,320),Image.Resampling.LANCZOS)
        top_width,top_height=top.size
        strip_height=top_height+16
        canvas=Image.new('RGB',(round(top_width*.5)+1,strip_height*len(SCOUT_TEXT_WINDOWS)))
        for i,(left,right) in enumerate(SCOUT_TEXT_WINDOWS):
            canvas.paste(top.crop((round(left*top_width),0,round(right*top_width),top_height)),(0,i*strip_height))
        self._initialize()
        result=self._detect_text(np.asarray(canvas),upright=True)
        tokens=[]
        for token in image_tokens(result,canvas.size):
            strip,y=divmod(round(token['y']*canvas.height),strip_height)
            if strip>=len(SCOUT_TEXT_WINDOWS) or y>=top_height:
                continue
            left,right=SCOUT_TEXT_WINDOWS[strip]
            x=left+token['x']*canvas.width/top_width
            if x<=right:
                tokens.append({**token,'x':x,'y':y/top_height*round(size[1]*.15)/size[1]})
        return scout_text_scene(tokens)

    def is_legend_selection(self, patches, width):
        if self._scout is None:
            from matchlog.screen_scout import ScreenScout
            self._scout=ScreenScout()
        return self._scout.is_selection(patches,width)

    def screen_hint(self, image, allow_fallback=True):
        """Only inspect the small header/HUD strips during play, using one OCR call."""
        if self._scout is None:
            from matchlog.screen_scout import ScreenScout
            self._scout=ScreenScout()
        hint=self._scout.hint(image)
        if hint=='other' and self._scout.requires_text:
            with self._lock:
                return self._text_scout(image.crop((0,0,image.width,round(image.height*.15))),image.size)
        if hint!='other' or not allow_fallback:
            return hint
        import numpy as np
        from PIL import Image
        w,h=image.size
        header=image.crop((int(.28*w),0,int(.71*w),int(.125*h)))
        hud=image.crop((int(.815*w),int(.025*h),int(.914*w),int(.075*h)))
        strips=[]
        range_title=image.crop((int(.82*w),int(.01*h),int(.99*w),int(.11*h)))
        for crop in [header,hud,range_title]:
            if crop.width>1000:
                crop=crop.resize((1000,round(crop.height*1000/crop.width)))
            strips.append(crop)
        canvas=Image.new('RGB',(max(s.width for s in strips),sum(s.height for s in strips)+40),'black')
        offset=0
        for strip in strips:canvas.paste(strip,(0,offset));offset+=strip.height+20
        with self._lock:
            self._initialize()
            result=self._detect_text(np.array(canvas))
        text='\n'.join(result.txts or [])
        if is_training_text(text):return 'training'
        if sum(label in text for label in ['总结','经验值','通行证'])>=2:
            return 'settlement'
        if '剩余小队' in text or '小队数量' in text:
            return 'hud'
        if hint=='other':
            lobby=self.read_lobby(image)
            if lobby.get('rank') and lobby.get('progress') is not None:return 'lobby'
        return 'other'

    def tokens(self, path, *, image=None):
        import numpy as np
        from PIL import Image
        with self._lock:
            self._initialize()
            if image is None:
                with Image.open(path) as source:
                    image=source.convert('RGB')
            img=image
            try:
                result=self._detect_text(np.asarray(img))
            except Exception as error:
                memory_error=isinstance(error,MemoryError) or any(
                    text in str(error).casefold() for text in ('bad allocation','failed to allocate','out of memory'))
                if not memory_error or max(img.size)<640:raise
            else:
                return image_tokens(result,img.size),img.size
            # Release the failed OCR call before retrying a smaller detector input.
            scale=min(.75,1280/max(img.size))
            resized=img.resize(tuple(max(1,round(edge*scale)) for edge in img.size),Image.Resampling.LANCZOS)
            self.last_read_pixels=resized.size
            self.last_read_route='full_smaller'
            result=self._detect_text(np.asarray(resized))
            return image_tokens(result,resized.size),img.size

    def read(self, path, *, image=None):
        from PIL import Image
        self.last_read_route='full'
        if image is None:
            with Image.open(path) as source:
                image=source.convert('RGB')
        elif image.mode!='RGB':
            image=image.convert('RGB')
        w,h=image.size
        self.last_read_pixels=image.size
        upright=w>=800 and 1.55<=w/h<=2.45 and image.getexif().get(274,1)==1
        cropped=self._read_rank_regions(image) if upright else None
        if cropped:
            self.last_read_route='rank_crop'
            return cropped
        tokens, size = self.tokens(path,image=image)
        parsed = parse(tokens)
        if parsed['kind']=='rank':
            parsed=confirm_rank_card(image,tokens,parsed)
        if parsed['kind']=='summary':
            missing_combat=any(p.get('kills') is None for p in parsed['players'])
            missing_header=any(parsed.get(field) is None for field in ('placement','squad_kills'))
            if missing_combat or missing_header:
                if missing_combat:parsed=self.recover_summary(image,parsed,tokens)
                if missing_header:parsed=self.recover_summary_header(image,parsed)
        if parsed['kind'] == 'hud':
            parsed.update(self.read_hud(image))
        elif parsed['kind']=='lobby':
            cropped=self.read_lobby(image)
            from matchlog.rp_progress import rank_total
            if (rank_total(cropped) is None and rank_total(parsed) is not None
                    and (cropped['progress'],cropped['capacity'])==(parsed['progress'],parsed['capacity'])):
                cropped['rank']=parsed['rank'];cropped['warnings']=[]
            parsed=cropped
        elif parsed['kind']=='profile':
            parsed=self.recover_profile(image,parsed)
        elif (parsed['kind']=='rank' and parsed.get('delta') is not None and not parsed.get('breakdown')
              and rank_card_is_fading(image,tokens)):
            parsed['tally_pending']=True
        key=game_key_from_tokens(tokens)
        return {**parsed, 'game_key':key, 'size': list(size), 'tokens': tokens}

    def _read_rank_regions(self, image):
        import numpy as np
        from matchlog.screen_scout import ScreenScout
        from matchlog.summary_navigation import page_tabs
        if self._scout is None:self._scout=ScreenScout()
        if not self._scout.has_rank_card(image):return None
        canvas,regions=rank_page_regions(image)
        with self._lock:
            self._initialize()
            try:
                output=self._detect_text(np.asarray(canvas),upright=True)
            except Exception as error:
                if not (isinstance(error,MemoryError) or any(text in str(error).casefold()
                        for text in ('bad allocation','failed to allocate','out of memory'))):
                    raise
                return None
        tokens=rank_page_tokens(output,image.size,regions)
        _,tabs=page_tabs(tokens)
        if not tabs:return None
        reliable=[t for t in tokens if t['score']>=.85]
        label=next((t for t in reliable if any(s in t['text'] for s in ('赢得的排位','失去的排位'))),None)
        if not label or rank_card_is_fading(image,tokens):return None
        rank=parse_rank(reliable)
        rank=confirm_rank_card(image,reliable,rank)
        if rank.get('rank_anchor_status')=='ambiguous':
            return None
        if (rank['tally_pending'] or not rank['breakdown'] or not rank['rank']
                or len(rank['combat_counts'])!=3 or rank['capacity'] is None or rank['progress'] is None
                or rank['delta'] is None or not 1<=(rank['placement'] or 0)<=30):
            return None
        key=game_key_from_tokens(tokens)
        if not key and any(t['y']>.90 and t['x']<.4 for t in tokens):return None
        left=next((unicodedata.normalize('NFKC',t['text']) for t in reliable
                   if re.fullmatch(r'IV|III|II|I',unicodedata.normalize('NFKC',t['text']))
                   and label['x']-.12<t['x']<label['x']-.01 and label['bottom']<t['y']<label['bottom']+.08),None)
        if left and not rank['rank'].endswith(' '+left):return None
        self.last_read_pixels=canvas.size
        return {**rank,'game_key':key,'size':list(image.size),'tokens':tokens}

    def recover_summary_header(self, image, parsed):
        """Try one higher-contrast header read when the full image missed a label or digit."""
        import numpy as np
        from PIL import ImageOps
        w,h=image.size
        box=(round(w*.61),round(h*.105),round(w*.982),round(h*.207))
        crop=ImageOps.autocontrast(ImageOps.grayscale(image.crop(box))).convert('RGB')
        with self._lock:
            self._initialize()
            try:
                result=self._detect_text(np.asarray(crop))
            except Exception:
                logging.debug('Optional settlement header retry failed',exc_info=True)
                return parsed
        tokens=[t for t in image_tokens(result,image.size,box[:2]) if t['score']>=.85]
        placement,squad_kills=parse_summary_header(tokens)
        for field,value in (('placement',placement),('squad_kills',squad_kills)):
            if parsed.get(field) is None and value is not None:parsed[field]=value
        return parsed

    def recover_profile(self,image,parsed):
        import numpy as np
        from PIL import Image
        regions={'matches':(.35,.245,.44,.294),'wins':(.45,.245,.54,.294),'top5':(.55,.245,.64,.294),
                 'highest_kills':(.459,.679,.486,.710)}
        for section,offset in [('lifetime',0),('season',.333)]:
            for field,box in regions.items():
                if parsed[section].get(field) is not None:continue
                x,y,r,b=box;crop=image.crop((round((x+offset)*image.width),round(y*image.height),round((r+offset)*image.width),round(b*image.height)))
                crop=crop.resize((crop.width*3,crop.height*3));canvas=Image.new('RGB',(crop.width+32,crop.height+32));canvas.paste(crop,(16,16))
                with self._lock:
                    self._initialize();result=self._detect_text(np.asarray(canvas))
                values=[(t.replace(',','').replace('，','').strip(),score) for t,score in zip(result.txts or [],result.scores or [])]
                valid=[v for v,score in values if score>=.78 and re.fullmatch(r'\d+(?:\.\d+)?[KkMm]?',v)]
                if len(valid)!=1:continue
                value=valid[0];scale={'k':1000,'m':1000000}.get(value[-1].lower(),1);number=float(value[:-1] if scale!=1 else value)*scale
                parsed[section][field]=int(number) if number.is_integer() else number
                if field!='highest_kills':parsed[section].setdefault('display_values',{})[field]=value.upper()
        return parsed

    def recover_summary(self,image,parsed,tokens):
        import numpy as np
        anchors=sorted([t for t in tokens if '造成伤害' in t['text']],key=lambda t:t['x'])
        for player,anchor in zip(parsed['players'],anchors):
            if player.get('kills') is not None:continue
            candidates=[t for t in tokens if anchor['x']-.025<=t['x']<anchor['x']+.125
                        and anchor['y']-.075<t['y']<anchor['y'] and re.fullmatch(r'\d{5,8}',t['text'])]
            for token in candidates:
                box=tuple(round(token[k]*(image.width if i%2==0 else image.height))+(-2 if i<2 else 2)
                          for i,k in enumerate(('x','y','right','bottom')))
                rgb=np.asarray(image.crop(box)).astype(float)
                mask=((rgb[:,:,0]>130)&(rgb[:,:,1]>45)&(rgb[:,:,1]<210)&
                      (rgb[:,:,0]>rgb[:,:,1]*1.2)&(rgb[:,:,2]<rgb[:,:,1]*.8)).astype('uint8')*255
                values=recover_triplet(token['text'],mask)
                if values is not None:player.update(zip(('kills','assists','knocks'),values));break
        parsed['warnings']=summary_warnings(parsed)
        return parsed

    def read_hud(self, image):
        """Anchor combat digits to the HUD, then read live RP and the session footer."""
        import numpy as np
        with self._lock:
            self._initialize()
            values=self._read_hud_combat(hud_crop(image))
            rank=hud_rank_crop(image);rank=rank.resize((rank.width*4,rank.height*4))
            rank_tokens=self._detect_text(np.array(rank))
            live_rp=parse_live_rp('\n'.join(rank_tokens.txts or []))
            footer=game_key_crop(image);footer=footer.resize((footer.width*2,footer.height*2))
            footer_tokens=self._detect_text(np.array(footer))
            game_key=parse_game_key('\n'.join(footer_tokens.txts or []))
        return {'kind':'hud',**dict(zip(HUD_FIELDS,values)),
                'live_rp':live_rp,'game_key':game_key,
                'warnings':['局内快照，不计入已结算战绩']}

    def _read_hud_combat(self, strip):
        import cv2
        import numpy as np
        from PIL import Image,ImageOps

        if self._hud_marker is None:
            path=Path(__file__).resolve().parents[1]/'ui/assets/detection/hud-participation.png'
            if not path.is_file():
                self._hud_marker=[]
            else:
                with Image.open(path) as source:
                    template=np.array(source.convert('L'))
                self._hud_marker=[(s,cv2.resize(template,None,fx=s,fy=s)) for s in (.90,1.,1.10)]
        width=round(strip.width*96/strip.height)
        row=strip.resize((width,96))
        gray=np.asarray(row.convert('L'))
        left,right=round(width*.255),round(width*.455)
        area=gray[8:75,left:right]
        best=(0,0,0,1)
        for scale,template in self._hud_marker:
            if any(outer<inner for outer,inner in zip(area.shape,template.shape)):continue
            _,score,_,point=cv2.minMaxLoc(cv2.matchTemplate(area,template,cv2.TM_CCOEFF_NORMED))
            if score>best[0]:best=(score,left+point[0],8+point[1],scale)
        score,x,y,scale=best
        if score<.82:
            return [None,None,None,self._read_damage_only(row,gray)]

        def number(left,right,cutoff=None,invert=False):
            box=(round(x+left*scale),round(y+2*scale),round(x+right*scale),round(y+35*scale))
            if box[0]<0 or box[2]>row.width:return None
            digit=row.crop(box)
            if invert:
                digit=ImageOps.invert(ImageOps.autocontrast(digit.convert('L'))).convert('RGB')
            elif cutoff is not None:
                digit=digit.convert('L').point(lambda value:255 if value>=cutoff else 0).convert('RGB')
            digit=digit.resize((digit.width*3,digit.height*3))
            padded=Image.new('RGB',(digit.width+12,digit.height+12))
            padded.paste(digit,(6,6))
            result=self._engine(np.asarray(padded),use_det=False,use_cls=False,use_rec=True)
            if result.txts and result.scores[0]>=.82 and re.fullmatch(r'[0-9]{1,5}',result.txts[0]):
                return int(result.txts[0])
            return None

        kills=number(-126,-94)
        if kills is None:kills=number(-126,-94,224)
        assists=None
        for left,right,cutoff in ((-40,-18,224),(-44,-16,None),(-46,-12,None),(-46,-12,208),(-42,-17,224)):
            assists=number(left,right,cutoff)
            if assists is not None:break
        participation=number(39,68,invert=True)
        damage=number(162,224)
        if damage is None:damage=number(162,224,224)

        def legacy_number(left,right):
            box=(round(strip.width*left),round(strip.height/6),round(strip.width*right),round(strip.height*.65))
            digit=strip.crop(box)
            result=self._detect_text(np.asarray(digit.resize((digit.width*4,digit.height*4))))
            values=[int(text) for text,confidence in zip(result.txts or [],result.scores or [])
                    if confidence>=.85 and re.fullmatch(r'[0-9]{1,5}',text)]
            return values[0] if len(values)==1 else None

        if assists is None:assists=legacy_number(.22,.28)
        if damage is None:damage=legacy_number(.564,.668)
        return [kills,assists,participation,damage]

    def _read_damage_only(self,row,gray):
        """Read the separately visible damage counter without inventing K/A."""
        import cv2
        import numpy as np
        from PIL import Image
        if self._damage_marker is None:
            path=Path(__file__).resolve().parents[1]/'ui/assets/detection/hud-damage.png'
            if not path.is_file():
                self._damage_marker=[]
                return None
            with Image.open(path) as source:
                template=np.asarray(source.convert('L'))
            self._damage_marker=[(s,cv2.resize(template,None,fx=s,fy=s)) for s in (.82,.90,1.,1.10,1.18)]
        left=round(row.width*.44)
        area=gray[8:73,left:round(row.width*.68)]
        best=(0,0,0,1,0)
        for scale,template in self._damage_marker:
            if any(outer<inner for outer,inner in zip(area.shape,template.shape)):continue
            _,score,_,(x,y)=cv2.minMaxLoc(cv2.matchTemplate(area,template,cv2.TM_CCOEFF_NORMED))
            if score>best[0]:best=(score,left+x,8+y,scale,template.shape[1])
        score,x,y,scale,marker=best
        if score<.89:return None
        digit=row.crop((x+marker,round(y-2*scale),min(row.width,x+marker+round(82*scale)),round(y+31*scale)))
        reads=[]
        for region in (digit,digit.convert('L').point(lambda value:255 if value>=211 else 0).convert('RGB')):
            region=region.resize((region.width*3,region.height*3))
            padded=Image.new('RGB',(region.width+12,region.height+12))
            padded.paste(region,(6,6))
            result=self._engine(np.asarray(padded),use_det=False,use_cls=False,use_rec=True)
            if result.txts and result.scores[0]>=.86 and re.fullmatch(r'[0-9]{1,5}',result.txts[0]):
                reads.append(int(result.txts[0]))
        return reads[0] if len(reads)==2 and reads[0]==reads[1] else None

    def read_lobby(self, image):
        import numpy as np
        from PIL import Image
        original=lobby_crop(image)
        crop=original
        if crop.width<700:crop=crop.resize((crop.width*2,crop.height*2))
        w,h=image.size;mode=image.crop((int(w*.025),int(h*.705),int(w*.235),int(h*.84)))
        mode=mode.resize((crop.width,round(mode.height*crop.width/mode.width)))
        canvas=Image.new('RGB',(crop.width,crop.height+mode.height+20));canvas.paste(crop,(0,0));canvas.paste(mode,(0,crop.height+20))
        with self._lock:
            self._initialize()
            result=self._detect_text(np.asarray(canvas))
        value=parse_lobby('\n'.join(result.txts or []))
        if value.get('rank') and value.get('progress') is not None:
            from matchlog.rank_badges import lobby_badge_rank
            confirmed=lobby_badge_rank(original,value['rank'])
            if confirmed:
                value['rank']=confirmed
                value['rank_anchor_status']='badge'
        return value

    def read_ranked_lobby_identity(self, image, *, verified_lobby=False, candidate=False):
        if not verified_lobby:
            hint=self.screen_hint(image,allow_fallback=False)
            if hint!='lobby' and not (candidate and hint in ('other','lobby_candidate')
                                      and self._lobby_navigation_visible(image)):
                return None
        rank=self.read_lobby(image)
        if rank.get('mode') in ('casual','event'):
            return {'mode':rank['mode']}
        name=self.read_lobby_name(image,expanded=True,candidate=candidate)
        from matchlog.rp_progress import rank_total
        return {'name':name,'rank':rank if rank_total(rank) is not None else None} if name else None

    def _lobby_navigation_visible(self, image):
        """Verify the lobby header independently when its mode template is unavailable."""
        import numpy as np
        w,h=image.size
        crop=image.crop((round(w*.18),0,round(w*.85),round(h*.116)))
        with self._lock:
            self._initialize()
            result=self._detect_text(np.asarray(crop))
        text='\n'.join(''.join(line.split()) for line,score in zip(result.txts or [],result.scores or []) if score>=.90)
        return '游玩' in text and any(label in text for label in ('挑战','通行证','传奇储物柜','商店'))

    def read_lobby_name(self, image, *, expanded=False, candidate=False):
        """OCR the owner's upper-left lobby card after scene classification."""
        import numpy as np
        from PIL import Image
        w,h=image.size
        boxes=(LOBBY_NAME_BOX,LOBBY_NAME_SEARCH_BOX) if expanded else (LOBBY_NAME_BOX,)
        for box in boxes:
            left,top,right,bottom=box
            crop=image.crop((round(left*w),round(top*h),round(right*w),round(bottom*h)))
            if not crop.width or not crop.height or crop.convert('L').getextrema()[1]<32:
                continue
            crop=crop.resize((crop.width*3,crop.height*3),Image.Resampling.BICUBIC)
            with self._lock:
                self._initialize()
                result=self._detect_text(np.asarray(crop))
            min_score=.93 if candidate or box==LOBBY_NAME_SEARCH_BOX else .88
            name=lobby_identity_name(image_tokens(result,crop.size),min_score=min_score)
            if name:
                return name
        return None

    def read_squad(self, image, self_name=None, *, roster_frame=True):
        from matchlog.heroes import HeroRecognizer,HERO_BOXES,NAME_BOXES,fractional_crop
        import numpy as np
        from PIL import Image
        canvas=Image.new('RGB',(500,210),'black')
        for i,box in enumerate(NAME_BOXES):
            if not roster_frame and i!=2:
                continue
            crop=fractional_crop(image,box);crop.thumbnail((480,60));canvas.paste(crop,(0,i*70))
        with self._lock:
            self._initialize();result=self._detect_text(np.asarray(canvas))
        names=['','','']
        for text,box in zip(result.txts or [],result.boxes if result.boxes is not None else []):
            slot=min(2,max(0,int(float(sum(p[1] for p in box))/4/70)))
            # Names may include the clan, which normalization removes later.
            if len(text)>len(names[slot]):names[slot]=text
        # A spectator's bottom portrait must never be relabelled as our own.
        own=bool(self_name and nickname(names[2])==nickname(self_name))
        if own:
            names[2]=self_name
        players=[]
        for slot,(name,box) in enumerate(zip(names,HERO_BOXES)):
            if not name or re.search(r'进化|等级|最高|NEXT|EVO',name,re.I):
                continue
            player={'name':name,'slot':slot}
            if roster_frame and (own or self_name is None):
                if self._heroes is None:
                    self._heroes=HeroRecognizer(self._hero_samples)
                face=self._heroes.observe(fractional_crop(image,box))
                # A visual match remains a candidate until this slot agrees in
                # another independent, verified frame of the same round.
                player.update(hero_candidate=face.get('hero'),hero_sample=face.get('hero_sample'))
            players.append(player)
        return players

    def confirm_hero_sample(self, ident, hero):
        from matchlog.heroes import HeroSamples
        if not self._hero_samples:return False
        return HeroSamples(self._hero_samples).confirm(ident,hero)


def parse(tokens):
    text = '\n'.join(t['text'] for t in tokens)
    if '账号等级' in text and '生涯' in text and ('账户进度' in text or '赛季' in text):return parse_profile(tokens)
    if is_training_text(text):return {'kind':'training','warnings':[]}
    if '参赛花费' in text and '排位赛' in text and ('大厅段位' in text or '小队段位' in text) and '排位点数' not in text:
        return {'kind':'intro','mode':'ranked','entry_cost':intro_entry_cost(tokens),
                'map_name':intro_map_name(tokens),'warnings':[]}
    if '伤害记录' in text and '击杀者' in text:
        return {'kind':'death_recap','warnings':[]}
    if '分段' in text and '目标' in text and '晋级至' in text and '赢得的排位' not in text:
        fraction=re.search(r'(\d[\d,]*)\s*/\s*(\d[\d,]*)',text[text.find('分段'):])
        return {'kind':'rank_overview','progress':int(fraction[1].replace(',','')) if fraction else None,
                'capacity':int(fraction[2].replace(',','')) if fraction else None,'delta':None,
                'warnings':['排位详情不是净加减分，请补充经验值页']}
    from matchlog.summary_navigation import page_tabs
    root,_=page_tabs(tokens)
    rank_card=any(label in text for label in ('排位点数','赢得的排位','失去的排位'))
    # A spectator HUD belongs to the player being watched, never to our backup.
    if root and not ('造成伤害' in text and ('给予' in text or '生存时间' in text)) and not rank_card:
        return {'kind':'postmatch','warnings':[]}
    if '剩余小队' in text or '小队数量' in text:
        return parse_hud(tokens)
    if '造成伤害' in text and ('给予' in text or '生存时间' in text):
        return parse_summary(tokens)
    if rank_card:
        return parse_rank(tokens)
    if '排位赛' in text and 'RP' in text:
        return parse_lobby(text)
    if detect_mode(text) and ('准备' in text or '游玩' in text):return parse_lobby(text)
    if ('剩余小队' in text or '小队数量' in text) and ('排位' in text or 'NZ' in text):
        return parse_hud(tokens)
    return {'kind': 'unknown', 'warnings': ['未识别为战绩、排位结算或局内画面']}


INTRO_MAP_LABELS = frozenset((
    '电力区域',  # Exact simplified-client label in the recorded S31 opening fixture.
    '电流区夜间','電流區夜間','暴风点','暴風點','残月','殘月',
    '奥林匹斯','奧林匹斯','世界边缘','世界邊緣',
    'E-District','E-District Night','Storm Point','Broken Moon','Olympus',"World's Edge",
))


def intro_map_name(tokens):
    """Read a documented map label immediately below the ranked opening header."""
    headers=[token for token in tokens if '排位赛' in token['text']
             and token.get('x',1)<.25 and token.get('y',1)<.17 and 'bottom' in token]
    candidates=set()
    for header in headers:
        for token in tokens:
            if (token.get('score',0)<.92 or not .025<=token.get('x',1)<.24
                    or token.get('right',1)>.32 or not 0<=token.get('y',1)-header['bottom']<=.075):
                continue
            label=' '.join(unicodedata.normalize('NFKC',token['text']).split())
            if label in INTRO_MAP_LABELS:
                candidates.add(label)
    return next(iter(candidates)) if len(candidates)==1 else None


def intro_entry_cost(tokens):
    """The intro can show a positive fee, while settlement shows the deduction."""
    def text(token):
        return unicodedata.normalize('NFKC',token['text']).replace('−','-').replace(' ','')
    for token in tokens:
        if token.get('score',1)<.90 or '参赛花费' not in text(token):continue
        inline=re.fullmatch(r'参赛花费:?[+\-]?(\d{1,3})RP',text(token),re.I)
        if inline:return int(inline[1])
        if text(token).rstrip(':')!='参赛花费':continue
        for neighbor in tokens:
            if (neighbor.get('score',1)>=.90 and abs(neighbor['y']-token['y'])<.016
                    and token['right']-.004<=neighbor['x']<=token['right']+.05):
                amount=re.fullmatch(r'[+\-]?(\d{1,3})RP',text(neighbor),re.I)
                if amount:return int(amount[1])
    return None


def parse_profile(tokens):
    def text(t):return unicodedata.normalize('NFKC',t['text']).replace(' ','')
    def number(value):
        value=value.replace(',','').replace('，','').strip()
        match=re.fullmatch(r'(\d+(?:\.\d+)?)([KkMm]?)',value)
        if not match:return None
        result=float(match[1])*({'k':1000,'m':1000000}.get(match[2].lower(),1))
        return int(result) if result.is_integer() else result
    def value_at(left,right,top,bottom):
        found=[t for t in tokens if left<=t['x']<right and top<=t['y']<bottom and number(t['text']) is not None]
        return number(found[0]['text']) if found else None
    header=next((t['text'] for t in tokens if .35<t['x']<.6 and .075<t['y']<.14),None)
    season=next((re.search(r'第?(\d+)赛季',text(t)) for t in tokens if .65<t['x'] and .14<t['y']<.20 and '赛季' in text(t)),None)
    mode=next((t['text'] for t in tokens if .34<t['x']<.63 and .14<t['y']<.20),None)
    sections={}
    for key,offset in [('lifetime',0),('season',.333)]:
        stats={k:value_at(a+offset,b+offset,.25,.29) for k,a,b in [('matches',.35,.44),('wins',.45,.54),('top5',.55,.64)]}
        stats['approximate_fields']=[k for k,a,b in [('matches',.35,.44),('wins',.45,.54),('top5',.55,.64)] if any(a+offset<=t['x']<b+offset and .25<=t['y']<.29 and re.search(r'[KkMm]$',t['text']) for t in tokens)]
        stats['display_values']={k:next((text(t).upper() for t in tokens if a+offset<=t['x']<b+offset and .25<=t['y']<.29 and number(t['text']) is not None),'') for k,a,b in [('matches',.35,.44),('wins',.45,.54),('top5',.55,.64)]}
        stats.update(damage=value_at(.35+offset,.49+offset,.50,.55),kills=value_at(.51+offset,.64+offset,.50,.55))
        for t in tokens:
            if not .34+offset<=t['x']<.65+offset:continue
            label=text(t)
            field=next((field for label_part,field in [('击杀/阵亡比','kd'),('阵亡','deaths'),('最高','highest_damage'),('平均','average_damage')] if label_part in label and .55<t['y']<.63),None)
            if field:
                match=re.search(r'(\d[\d,]*(?:\.\d+)?)$',label)
                if match:stats[field]=number(match[1])
            if .67<t['y']<.76:
                labels={'最高击杀':'highest_kills','最长连胜':'win_streak','击倒':'knocks','助攻':'assists','急救队友次数':'revives','重生队友次数':'respawns'}
                field=next((value for name,value in labels.items() if label.startswith(name)),None)
                if field:
                    right=.485+offset if t['x']<.49+offset else .645+offset
                    inline=re.search(r'(\d[\d,]*(?:\.\d+)?)$',label)
                    stats[field]=number(inline[1]) if inline else value_at(t.get('right',t['x'])+.002,right,t['y']-.012,t['y']+.015)
        sections[key]=stats
    level=value_at(.10,.19,.35,.43)
    def fraction_in(left,right,top,bottom):
        for t in tokens:
            if left<t['x']<right and top<t['y']<bottom:
                match=re.search(r'(\d+)\s*/\s*(\d+)',t['text'])
                if match:return [int(match[1]),int(match[2])]
        return [None,None]
    prestige= fraction_in(.08,.15,.53,.59)
    career=fraction_in(.16,.28,.53,.59)
    xp=fraction_in(.46,.58,.30,.36)
    return {'kind':'profile','name':header,'mode_label':mode,'selected_season':'S'+season[1] if season else None,
            'account_level':level,'prestige':prestige[0],'prestige_max':prestige[1],'career_level':career[0],
            'career_level_max':career[1],'xp':xp[0],'xp_next':xp[1],**sections,'warnings':[]}


def is_training_text(text):
    return any(t in text for t in ('射击场','靶场')) and any(t in text for t in ('射击数','友军误击','全面战斗'))


def detect_mode(text):
    if re.search(r'排位赛|排位联赛|積分賽|参赛花费|赢得的排位',text):return 'ranked'
    if re.search(r'团队死斗|团队死亡竞赛|控制模式|封锁|混合模式|限时|活动模式|军火库|武装危机|捉迷藏',text):return 'event'
    if re.search(r'三人赛|双人赛|单人赛|普通匹配',text):return 'casual'
    return None


def parse_lobby(text):
    text=unicodedata.normalize('NFKC',text)
    rank=re.search(r'(新秀|青铜|白银|黄金|白金|铂金|钻石)\s*(IV|III|II|I|[1-4])(?![IV\d])',text.upper())
    name=(rank[1]+' '+{'1':'I','2':'II','3':'III','4':'IV'}.get(rank[2],rank[2])) if rank else next((n for n in ['大师','猎杀者'] if n in text),None)
    fraction=re.search(r'(\d[\d,]*)\s*/\s*(\d[\d,]*)\s*RP',text,re.I)
    progress=int(fraction[1].replace(',','')) if fraction else None
    capacity=int(fraction[2].replace(',','')) if fraction else None
    if progress is not None and not 0<=progress<=capacity:
        progress=capacity=None
    return {'kind':'lobby','mode':detect_mode(text) or ('ranked' if name and progress is not None else None),'rank':name,'progress':progress,'capacity':capacity,
            'remaining':capacity-progress if progress is not None else None,'total_rp':None,'source':'lobby',
            'warnings':[] if name and progress is not None else ['大厅排位卡片未读全']}


def recover_triplet(text,mask):
    """Read separators from their visible diagonal geometry, never infer them from a '1'."""
    import cv2,numpy as np
    n,labels,stats,_=cv2.connectedComponentsWithStats(mask)
    items=sorted([(i,*s) for i,s in enumerate(stats[1:],1) if s[4]>=8],key=lambda s:s[1])
    if len(items)!=len(text) or not text.isdigit():return None
    height=max(s[4] for s in items);slashes=[]
    for j,(i,x,y,w,h,area) in enumerate(items):
        ys,xs=np.where(labels==i)
        slope=float(np.polyfit(ys,xs,1)[0]) if len(set(ys))>1 else 0
        if h>=height*.7 and .18<w/h<.56 and -.65<slope<-.20 and area/(w*h)<.57:slashes.append(j)
    if len(slashes)!=2:return None
    a,b=slashes;parts=(text[:a],text[a+1:b],text[b+1:])
    if any(not p or len(p)>2 for p in parts):return None
    return tuple(map(int,parts))

def summary_warnings(summary):
    players=summary.get('players',[])
    return ['部分队员数据未读全，请查看原图'] if len(players)!=3 or any(not p.get('name') or any(p.get(k) is None for k in ('kills','assists','knocks','damage')) for p in players) else []

def parse_summary(tokens):
    players = []
    # Three cards are located by their damage labels, not absolute screen pixels.
    anchors = sorted([t for t in tokens if '造成伤害' in t['text']], key=lambda t: t['x'])
    for anchor in anchors:
        left = anchor['x'] - .025
        group = sorted([t for t in tokens if left <= t['x'] < left+.15 and .22 < t['y'] < .85], key=lambda t: t['y'])
        slash = next((t for t in group if re.fullmatch(r'\d+\s*/\s*\d+\s*/\s*\d+', t['text'])), None)
        names = [t for t in group if .24 < t['y'] < anchor['y']-.055 and not re.search(r'击杀|助攻|击倒|^\d|^ID', t['text'])]
        name=min(names,key=lambda t:abs(t['y']-(anchor['y']-.13))) if names else None
        player = {'name': name['text'] if name else '', 'kills': None, 'assists': None, 'knocks': None,
                  'damage': None, 'survival': None, 'revives': None, 'respawns': None, 'deaths': None}
        if slash:
            player.update(zip(('kills','assists','knocks'), map(int, slash['text'].split('/'))))
        for key, label in [('damage','造成伤害'),('survival','生存时间'),('revives','急救'),('respawns','重生')]:
            header = next((t for t in group if label in t['text']), None)
            if header:
                value = next((t for t in group if header['bottom']-.012 < t['y'] < header['bottom']+.045
                              and (re.fullmatch(r'\d{1,2}:\d{2}', t['text']) if key=='survival' else integer(t['text']) is not None)), None)
                if value:
                    player[key] = value['text'] if key=='survival' else integer(value['text'])
        players.append(player)
    placement,squad_kills=parse_summary_header(tokens)
    summary={'kind':'summary','placement':placement,'squad_kills':squad_kills,'players':players}
    return {**summary,'warnings':summary_warnings(summary)}


def parse_summary_header(tokens):
    # Each number belongs to its top-right field, never to the heading next to it.
    header = sorted(({**t,'text':unicodedata.normalize('NFKC',t['text']).strip()} for t in tokens
                     if .08<t['y']<.23 and t['x']>.60),key=lambda t:t['x'])
    place_label='小队排名'
    total_label='小队总击杀'
    place_anchor=next((t for t in header if place_label in t['text']),None)
    total_anchor=next((t for t in header if total_label in t['text']),None)
    placement_limit=total_anchor['x'] if total_anchor else .89

    def right_number(anchor, pattern, limit):
        for t in header:
            if (anchor['right']-.015<=t['x']<min(limit,anchor['right']+.10)
                    and abs(t['y']-anchor['y'])<.035):
                found=re.fullmatch(pattern,t['text'])
                if found:return int(found[1])
        return None

    def header_number(anchor, label, pattern, limit):
        if anchor is None:return None
        suffix=anchor['text'].split(label,1)[1]
        found=re.match(r'\s*:?\s*'+pattern+rf'(?=\s*(?:$|{place_label}|{total_label}))',suffix)
        return int(found[1]) if found else right_number(anchor,pattern,limit)

    placement=header_number(place_anchor,place_label,r'(?:第\s*)?(\d{1,2})',placement_limit)
    if place_anchor is None:
        for t in header:
            if t['x']>=placement_limit:break
            explicit=re.fullmatch(r'第\s*(\d{1,2})',t['text'])
            if explicit:
                placement=int(explicit[1])
            elif t['text']=='第':
                placement=right_number(t,r'(\d{1,2})',placement_limit)
            if placement is not None:break
    if placement is not None and not 1<=placement<=30:placement=None
    squad_kills=header_number(total_anchor,total_label,r'(\d+)',1)
    return placement,squad_kills


def rank_card_is_fading(image, tokens):
    """A dim RP card beside fully visible XP tabs can still show an animated score."""
    xp=next((t for t in tokens if t.get('score',0)>=.90 and t['text']=='经验值'
             and .43<t['x']<.68 and .065<=t['y']<.125),None)
    if not xp:return False
    label=next((t for t in tokens if any(s in t['text'] for s in ('赢得的排位','失去的排位'))),None)
    if not label:return False
    import numpy as np
    def white_level(token):
        box=tuple(round(token[k]*image.size[i%2]) for i,k in enumerate(('x','y','right','bottom')))
        if box[2]<=box[0] or box[3]<=box[1]:return 0
        rgb=np.asarray(image.crop(box).convert('RGB'))
        return float(np.percentile(rgb.min(axis=2),98))
    tab_white=white_level(xp)
    card_white=white_level(label)
    return tab_white>=140 and 0<card_white<128 and card_white<tab_white*.65


def confirm_rank_card(image, tokens, parsed):
    if parsed.get('rank_anchor_status')!='ambiguous':
        return parsed
    from matchlog.rank_badges import confirm
    label=next((t for t in tokens if any(s in t['text'] for s in ('赢得的排位','失去的排位'))),None)
    return confirm(image,label,parsed)


def parse_rank(tokens):
    tokens = [{**t,'raw_text':t['text'],'text':unicodedata.normalize('NFKC',t['text']).replace('−','-')} for t in tokens]
    text = '\n'.join(t['text'] for t in tokens)
    # Locate the rank card by its label. Full XP pages contain unrelated +XP numbers.
    label=next((t for t in tokens if any(s in t['text'] for s in ('赢得的排位','失去的排位'))),None)
    delta=None
    progress = None
    capacity = None
    if label:
        height=max(label['bottom']-label['y'],.005)
        width=max(label['right']-label['x'],.015)
        candidates=[t for t in tokens if re.fullmatch(r'(?:[+\-]\s*\d+|0)',t['text'])
                    and label['y']-height*2.2<t['y']<label['y']+height*.3
                    and abs(t['x']-label['x'])<width*.7]
        if candidates:
            nearest=min(candidates,key=lambda t:abs(t['bottom']-label['y']))
            delta=integer(nearest['text'].replace(' ',''))
        captions=[]
        for t in tokens:
            if label['bottom']-height*.1<t['y']<label['bottom']+height*3.5 and label['x']-width*.5<t['x']<label['right']+width:
                found=re.fullmatch(r'(\d[\d,]*)\s*(?:排位点数|RP)?',t['text'],re.I)
                if found:
                    captions.append((t,int(found[1].replace(',',''))))
        if captions:
            # Lower row holds 0 and the division capacity; the bar itself holds progress.
            bottom=max(t['y'] for t,n in captions)
            lower=[(t,n) for t,n in captions if t['y']>=bottom-height*.35]
            capacity=max((n for t,n in lower),default=None)
            upper=[(t,n) for t,n in captions if t['y']<bottom-height*.35]
            if upper:
                progress=min(upper,key=lambda item:item[0]['y'])[1]
            elif len(lower)>=2:
                progress=min(lower,key=lambda item:item[0]['x'])[1]
            if capacity is not None and progress is not None and progress>capacity:
                progress=None
    nxt = re.search(r'下一段位[:：]?\s*([^\n]+)', text)
    next_rank = nxt[1].strip() if nxt else None
    tiers='新秀|青铜|白银|黄金|白金|铂金|钻石'
    label_pattern=rf'({tiers})\s*(IV|III|II|I)(?![IV])'
    card_next=None
    doubtful_footer=False
    if not next_rank and label:
        next_token=next((t for t in tokens if t['y']>label['bottom'] and t['y']<label['bottom']+max(label['bottom']-label['y'],.01)*12
                        and re.search(label_pattern,t['text'])),None)
        if next_token:
            found=re.search(label_pattern,next_token['text']);next_rank=found[0]
            raw_numeral=re.search(rf'(?:{tiers})\s*([IVⅠⅡⅢⅣ]+)',next_token['raw_text'])
            if raw_numeral:
                roman=raw_numeral[1]
                doubtful_footer=bool(re.search(r'[IV]',roman) and re.search(r'[ⅠⅡⅢⅣ]',roman))
            # The next badge and footer often acquire the same OCR stroke.
            # The current badge determines the division within a tier.
            h=max(label['bottom']-label['y'],.005);w=max(label['right']-label['x'],.015)
            badges=[t for t in tokens if re.fullmatch(r'IV|III|II|I',t['text']) and
                    label['bottom']<t['y']<label['bottom']+h*4]
            left=next((t['text'] for t in badges if label['x']-w<t['x']<label['x']-.01),None)
            right=next((t['text'] for t in badges if label['right']+.01<t['x']<label['right']+w),None)
            if left in ('IV','III','II') and right=={'IV':'III','III':'II','II':'I'}[left]:
                card_next=found[1]+right
            elif left=='I' and right=='IV':
                card_next=found[1]+right
    rank = None
    if next_rank:
        divisions=[f'{tier}{div}' for tier in ['新秀','青铜','白银','黄金','白金','钻石'] for div in ['IV','III','II','I']]+['大师']
        normalized=re.sub(r'\s+','',next_rank).replace('铂金','白金')
        if card_next and card_next in divisions:
            normalized=card_next;next_rank=re.sub(r'([IV]+)$',r' \1',normalized)
        if normalized in divisions and divisions.index(normalized)>0:
            rank=divisions[divisions.index(normalized)-1]
            rank=re.sub(r'([IV]+)$',r' \1',rank)
    protection=parse_demotion_protection(tokens,label)
    breakdown=parse_rank_breakdown(tokens)
    if (breakdown.get('protection_rp') or 0)>0 or (protection is not None and breakdown.get('protection_rp') is not None):
        protection={**(protection or {}),'refund':breakdown['protection_rp']}
    from matchlog.rp_reconcile import breakdown_delta
    calculated=breakdown_delta({'breakdown':breakdown})
    tally_pending=bool(breakdown and (calculated is None or calculated!=delta))
    return {'kind':'rank','delta':delta,'progress':progress,'capacity':capacity,'rank':rank,
            'placement':int(re.search(r'第\s*(\d{1,2})(?!\d)',text)[1]) if '定级' in text and re.search(r'第\s*(\d{1,2})(?!\d)',text) else None,
            'next_rank':next_rank,'total_rp':None,'breakdown':breakdown,'tally_pending':tally_pending,
            'demotion_protection':protection,'combat_counts':rank_combat_counts(tokens) if breakdown else {},
            **({'rank_anchor_status':'ambiguous'} if doubtful_footer and not card_next else {}),
            'warnings':[] if delta is not None else ['加减分未读取成功，等待稳定画面或手动截图']}


def parse_demotion_protection(tokens, label):
    if not label:return None
    height=max(label['bottom']-label['y'],.005)
    card=[t for t in tokens if label['bottom']-.01<=t['y']<=label['bottom']+height*7
          and label['x']-.06<=t['x']<=label['right']+.10]
    phrase=''.join(t['text'] for t in card).replace(' ','')
    if '降级保护' not in phrase:return None
    for token in card:
        count=re.search(r'(?<!\d)([0-9])\s*/\s*([1-9])(?!\d)',token['text'])
        if count and int(count[1])<=int(count[2]):
            return {'remaining':int(count[1]),'capacity':int(count[2])}
    return {}


def parse_rank_breakdown(tokens):
    """Read the game's individual RP rows, never infer a missing fee as zero."""
    if not any(t['y']>.44 and '总计' in t['text'] and ('RP' in t['text'] or '排位点数' in t['text'])
               and any(label in t['text'] for label in ('战斗','奖励','定级')) for t in tokens):
        return {}
    def value(label, start, end, exact=False):
        marker=next((t for t in tokens if start<t['x']<start+.10 and t['y']>.44
                     and (t['text']==label if exact else label in t['text'])),None)
        if not marker:return None
        for t in sorted(tokens,key=lambda t:t['x'],reverse=True):
            if marker['x']<=t['x']<end and abs(t['y']-marker['y'])<.018:
                match=re.search(r'([+\-]?[\dO,]+)\s*(?:RP|排位点数)\s*$',t['text'],re.I)
                if match:return int(match[1].upper().replace('O','0').replace(',',''))
        return None
    def total(label, rows, start, end):
        header=value(label,start,end)
        parts=[value(r,start,end) for r in rows]
        return header if header is not None else sum(parts) if all(v is not None for v in parts) else None
    fee=value('参赛花费',.63,1.0)
    placement=value('定级',.63,1.0,exact=True)
    placement_total=value('定级',.63,1.0)
    if placement_total is not None and fee is not None:
        placement=placement_total-fee
    protection=value('降级保护',.30,.66)
    bonus_rows=('挑战者','前5名连胜')+('降级保护',) if protection is not None else ('挑战者','前5名连胜')
    return {'combat_rp':total('战斗',('击杀','助攻','参与','击杀上限调整'),0,.33),
            'bonus_rp':total('奖励',bonus_rows,.30,.66),
            'placement_rp':placement,'entry_cost':-fee if fee is not None and fee<=0 else None,
            'protection_rp':protection}


def rank_combat_counts(tokens):
    """Read the count column left of the RP amounts on the combat breakdown."""
    reliable=[t for t in tokens if t.get('score',1)>=.90 and t['y']>.44 and t['right']<.27]
    counts={}
    for label,field in zip(('击杀','助攻','参与'),HUD_FIELDS):
        marker=next((t for t in reliable if t['x']<.10 and t['text'].strip()==label),None)
        if not marker:continue
        found=set()
        for token in reliable:
            if not (.105<token['x']<.26 and abs(token['y']-marker['y'])<.014):continue
            text=token['text'].replace(' ','')
            match=re.fullmatch(r'\d{1,2}',text)
            if not match and field=='participation':
                match=re.fullmatch(r'(\d{1,2})[xX×]50%=(?:\d{1,2})(?:\.5|\.0)?',text)
            if match:found.add(int(match[1] if match.lastindex else match[0]))
        if len(found)==1:counts[field]=found.pop()
    return counts


def parse_hud(tokens):
    # HUD K/A/participation are not the summary K/A/knocks. Never turn HUD into a final match.
    row = sorted([t for t in tokens if t['x']>.72 and .065<t['y']<.13],key=lambda t:t['x'])
    values=[]
    for t in row:
        nums=re.findall(r'\d+',t['text'])
        if len(nums)==1:
            values.append(int(nums[0]))
    result = {'kind':'hud','kills':None,'assists':None,'participation':None,'damage':None,
              'warnings':['局内快照，不计入已结算战绩']}
    if len(values)==4:
        result.update(zip(('kills','assists','participation','damage'), values))
    return result
