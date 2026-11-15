"""Plan post-elimination menu navigation from visible labels, one click per frame."""
import threading


def page_tabs(tokens):
    labels=[t for t in tokens if t.get('score',0)>=.90 and 0<=t.get('y',1)<.13]
    def find(label,left,right,top,bottom):
        return next((t for t in labels if t['text']==label and left<t['x']<right and top<=t['y']<bottom),None)
    root={name:find(name,.28,.68,0,.065) for name in ('排位','实况','总结')}
    if not all(root.values()):root={}
    summary=find('总结',.27,.55,.065,.125)
    xp=find('经验值',.43,.68,.065,.125)
    sub={'summary':summary,'rank':xp} if summary and xp else {}
    return root,sub


def navigation_target(result, recorded):
    root,sub=page_tabs(result.get('tokens',[]))
    kind=result.get('kind')
    if {'summary','rank'}<=recorded:return None
    if kind in ('postmatch','rank_overview') and root:return 'root-summary',root['总结']
    if kind=='death_recap':
        if sub:return 'summary',sub['summary']
        if root:return 'root-summary',root['总结']
    if kind=='summary' and 'summary' in recorded and sub:return 'rank',sub['rank']
    if kind=='rank' and 'rank' in recorded and 'summary' not in recorded and sub:return 'summary',sub['summary']
    return None


class SummaryNavigator:
    def __init__(self):self.rounds={};self.lock=threading.Lock()

    def step(self,result,recorded,round_id,now,click):
        with self.lock:return self._step(result,recorded,round_id,now,click)

    def _step(self,result,recorded,round_id,now,click):
        target=navigation_target(result,recorded)
        if not target or not round_id:return None
        name,token=target
        state=self.rounds.setdefault(round_id,{'last':float('-inf'),'attempts':{}})
        if now-state['last']<1.5 or state['attempts'].get(name,0)>=3:return None
        if not click(token):return None
        state['last']=now;state['attempts'][name]=state['attempts'].get(name,0)+1
        if len(self.rounds)>16:self.rounds.pop(next(iter(self.rounds)))
        return name
