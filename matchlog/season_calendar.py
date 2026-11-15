"""Explicit timezone-aware transitions. Unknown future dates are never guessed."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import re

CHINA=timezone(timedelta(hours=8))
DEFAULT={'schema':1,'verified_on':'2026-11-04','current':{'season':'S31','split':'1'},
         'source':'https://www.ea.com/games/apex-legends/apex-legends/news/cipher-patch-notes',
         'transitions':[],'note':'当前 S31 已核对；下一次确切赛段／赛季切换时间待官方确认。'}


def aware_time(value):
    dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if dt.tzinfo is None:raise ValueError('日程时间必须包含时区，例如 +08:00 或 Z')
    return dt.astimezone(timezone.utc)


class SeasonCalendar:
    def __init__(self, root):
        self.path=Path(root)/'season_schedule.json'
        self.data=json.loads(json.dumps(DEFAULT))
        self.error=None
        if self.path.exists():
            try:self.data=self.validate(json.loads(self.path.read_text(encoding='utf-8')))
            except (ValueError,KeyError,TypeError) as e:self.error='时间表无效：'+str(e)

    @staticmethod
    def validate(data):
        if not isinstance(data,dict) or not isinstance(data.get('transitions'),list):raise ValueError('缺少 transitions 数组')
        if len(data['transitions'])>200:raise ValueError('时间表最多包含 200 次切换')
        events=[];seen=set()
        for event in data['transitions']:
            if not isinstance(event,dict) or 'at' not in event:raise ValueError('每条切换须包含时间、赛季和赛段')
            if not re.fullmatch(r'S\d{1,3}',str(event.get('season',''))):raise ValueError('赛季格式应为 S31')
            if str(event.get('split')) not in ['1','2']:raise ValueError('赛段应为 1 或 2')
            at=aware_time(event['at']).isoformat()
            if at in seen:raise ValueError('切换时间不能重复')
            seen.add(at);events.append({'at':at,'season':event['season'],'split':str(event['split']),
                                       'source':str(event.get('source','导入时间表'))[:256]})
        return {**DEFAULT,**data,'transitions':sorted(events,key=lambda e:e['at'])}

    def replace(self, data):
        valid=self.validate(data)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(valid,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(self.path);self.data=valid;self.error=None

    def add_local(self, season, split, value):
        dt=datetime.fromisoformat(str(value))
        if dt.tzinfo is None:dt=dt.replace(tzinfo=CHINA)  # The input is explicitly labelled Beijing time.
        event={'season':season.upper().strip(),'split':str(split),'at':dt.astimezone(timezone.utc).isoformat(),'source':'用户录入'}
        self.replace({**self.data,'transitions':self.data['transitions']+[event]})

    def due(self, now=None):
        now=now or datetime.now(timezone.utc)
        now=aware_time(now.isoformat())
        past=[e for e in self.data['transitions'] if aware_time(e['at'])<=now]
        return past[-1] if past else None

    def status(self, now=None):
        now=now or datetime.now(timezone.utc)
        next_event=next((e for e in self.data['transitions'] if aware_time(e['at'])>now),None)
        return {'timezone':'Asia/Shanghai','verified_on':self.data['verified_on'],'source':self.data['source'],
                'transitions':self.data['transitions'],'next':next_event,'error':self.error,
                'message':self.error or ('已设置下一次切换' if next_event else '下一次切换时间待确认，保留当前赛季／赛段')}
