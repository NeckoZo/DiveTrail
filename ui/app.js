'use strict';
const $=(s,root=document)=>root.querySelector(s), $$=(s,root=document)=>[...root.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(v,digits=1)=>v==null?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:digits});
const signed=v=>v==null?'—':`${v>=0?'+':''}${fmt(v)}`;
const tone=v=>v==null?'':v>=0?'positive':'negative';
const nicknameKey=key=>key;
const nicknameLabel=name=>name;
const norm=v=>{const key=String(v??'').normalize('NFKC').replace(/^\s*\[[^\]]+\]\s*/,'').replace(/\s+/g,'').toLowerCase();return typeof nicknameKey==='function'?nicknameKey(key):key;};
const displayName=v=>{const name=String(v??'').replace(/^\s*\[[^\]]+\]\s*/,'').trim();return typeof nicknameLabel==='function'?nicknameLabel(name,norm(name)):name;};
const clock=v=>v?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v)):'—';
const fullClock=v=>v?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(v)):'—';
const detailClock=v=>fullClock(v).replace(/^\d{4}\D/,'');
const dateKey=v=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(v));
const localTimeInput=v=>v?new Date(new Date(v).getTime()+8*3600000).toISOString().slice(0,16):'';
const modeName=v=>({ranked:'排位',casual:'匹配',event:'活动'})[v]||'待识别';
const avg=values=>values.length?values.reduce((a,b)=>a+b,0)/values.length:null;
const checks=['auto_season','start_on_login','debug_mode','low_impact','auto_cleanup','reduce_motion','show_hero_board','sound_end_enabled','sound_summary_enabled','sound_rank_enabled'];
const retentionFields=['cleanup_temporary_days','cleanup_review_days','cleanup_original_days','highlight_days'];
const highlightDefaults={highlight_kills:5,highlight_team_kills:10,highlight_damage:1500,highlight_rp:200};
const friendFields=['friend_one','friend_two'];
const defaults={self_name:'',friends:[],season:'S31',split:'1',game_mode:'ranked',theme:'dark',auto_switch:false,auto_season:true,start_on_login:false,debug_mode:true,low_impact:false,auto_cleanup:true,cleanup_temporary_days:1,cleanup_review_days:2,cleanup_original_days:3,highlight_days:0,...highlightDefaults,reduce_motion:false,show_hero_board:true,settlement_tone:'rank',sound_end_enabled:true,sound_summary_enabled:true,sound_rank_enabled:true};
let state={config:{...defaults},matches:[],teammates:[],running:false,runtime:{},rank_progress:{points:[],current:null,total_rp:null},profile:null};
let view='overview',filter='all',currentMatch=null,lastSerialized='',toastTimer,stateEpoch=0,pendingMutations=0,pollRevision=null;
let refreshInFlight=null,queuedForcedRefresh=null;
let heroBag=[],heroCatalog=null,currentHero=null,currentArt=null,heroLoad=0,skinGallery=null;
const portraitIds=new Map();
const heroArtCycles=new Map();
let selectedPlayer='',heroMatchFilter=null;
let profileChangeShown=null,firstRunShown=false,whatsNewShown=null,whatsNewOpening=false;
let mergeDraft=null,mergeBusy=null,mergeRevision=0,retryBusy=false;
const own=m=>m.summary?.players?.find(p=>norm(p.name)===norm(state.config.self_name));
const validDelta=m=>m.rank?.delta!=null;
const effectiveRp=m=>usesRP(m)?m.rank?.delta??m.hud_rp_evidence?.value??null:null;
function matchRp(m, detail=false){
  const value=effectiveRp(m);
  return value==null?'—':`${signed(value)}${detail?' RP':''}`;
}
const pendingTime=m=>m.record_origin==='upload'&&!m.time_corrected;
const pendingTimeLabel=(m,now=Date.now())=>pendingTime(m)&&now-m.created*1000<86400000;
const placeClass=n=>Number.isInteger(n)&&n>=1&&n<=5?'place-'+n:'';
const heroName=v=>v==='Axle'?'艾瑟儿':v||'—';
const usesRP=m=>!['casual','event'].includes(m.mode);
const winCount=(wins,placementGames)=>placementGames ? `${fmt(wins,0)} 场` : '—';
const complete=m=>m.status?.complete??(m.record_source!=='last_snapshot'&&m.summary?.placement!=null&&m.summary?.players?.length===3&&!!own(m)&&m.summary.players.every(p=>p.name&&['kills','assists','knocks','damage'].every(k=>p[k]!=null))&&(['casual','event'].includes(m.mode)||validDelta(m)));
function statusCode(m){
  if(m.status?.code==='ongoing')return 'ongoing';
  if(m.record_source==='last_snapshot'||m.status?.snapshot||(m.mode==='ranked'&&m.rank?.delta==null&&Number.isInteger(m.hud_rp_evidence?.value)))return 'snapshot';
  if(m.status?.code)return m.status.code;
  if(m.status?.label==='进行中')return 'ongoing';
  return complete(m)?'recorded':'incomplete';
}
function statusLabel(m){
  const status=visibleStatus(m);
  if(status==='recorded')return m.corrected||m.status?.label==='已修正'?'已修正':'已记录';
  return {incomplete:'待补全',snapshot:'最后快照',ongoing:'进行中'}[status]||'待补全';
}
function visibleStatus(m){
  const status=statusCode(m);
  return status!=='ongoing'&&pendingTimeLabel(m)?'incomplete':status;
}
function matchesStatus(m,filter){
  return filter==='all'||visibleStatus(m)===filter;
}
function matchStatus(m){
  const status=visibleStatus(m);
  const tone=status==='snapshot'?'snapshot-tag':status==='incomplete'?'pending':'';
  const missing=m.status?.missing??m.audit?.missing??[];
  const reasons=[];
  if(status==='incomplete'){
    if(missing.length)reasons.push(`缺 ${missing.join(' / ')}`);
    if(pendingTimeLabel(m))reasons.push('时间待确认');
  }
  return `<span class="state-tag ${tone}">${esc(statusLabel(m))}</span>${reasons.length?`<small class="audit-missing">${esc(reasons.join(' · '))}</small>`:''}`;
}
function toast(message,error=false,undo=null){
  const el=$('#toast');
  el.textContent=message;
  el.classList.toggle('error',error);
  el.hidden=false;
  if(undo){
    const button=document.createElement('button');
    button.type='button';
    button.textContent='撤销';
    button.addEventListener('click',()=>{
      button.disabled=true;
      action(async()=>{applyState(await call('restore_match',undo));toast('对局已恢复');});
    });
    el.append(' ',button);
  }
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>el.hidden=true,undo?10000:3800);
}
async function call(method,...args){
  if(!window.pywebview?.api)throw new Error('请在桌面版操作');
  const mutation=!['state','poll_state','evidence','backup_evidence','capture_evidence','hero_gallery','hero_artwork','capture_diagnostics','map_rotation','cloud_log_admin_status','export_cloud_diagnostics'].includes(method);
  if(mutation){stateEpoch++;pendingMutations++;}
  try{return await window.pywebview.api[method](...args);}finally{if(mutation)pendingMutations--;}
}
async function action(fn){try{await fn();}catch(e){toast(e.message||String(e),true);}}
function applyState(value){state=value;pollRevision=null;lastSerialized=JSON.stringify(value);render();}
function applyTheme(theme){document.documentElement.dataset.theme=['light','slate'].includes(theme)?theme:'dark';}
function navigate(next){
  const entering=view!==next;
  view=next;
  const target=$(`#${next}-view`);
  if(entering&&!state.config.reduce_motion){
    target.classList.add('view-enter');
    target.addEventListener('animationend',()=>target.classList.remove('view-enter'),{once:true});
  }
  for(const name of ['overview','matches','teammates','maps'])$(`#${name}-view`).hidden=name!==next;
  $$('.nav[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav===next));
  $('#page-title').textContent=({overview:'战绩概览',matches:'对局记录',teammates:'玩家档案',maps:'地图轮换'})[next];
  if(next==='overview'){
    MapView.tick();
    requestAnimationFrame(()=>{renderChart();resizeChartText();fitHeroArt();});
  }
  if(next==='matches')refresh();
  if(next==='maps')MapView.enter(state);
  if(next==='teammates')renderPlayerHeroes();
}
function loadHeroes(){
  if(!heroCatalog)heroCatalog=fetch('hero-lines.json').then(response=>response.json()).then(heroes=>{
    for(const hero of heroes)if(/^[a-z0-9-]+$/.test(hero.id))portraitIds.set(hero.name,hero.id);
    $('#hero-names').innerHTML=heroes.map(h=>`<option value="${esc(h.name)}">`).join('');
    return heroes;
  }).catch(error=>{heroCatalog=null;throw error;});
  return heroCatalog;
}
async function decodeHeroArt(src){
  if(!src)throw new Error('立绘文件不可用');
  const image=new Image();
  image.id='hero-art';
  image.className='hero-art';
  image.src=src;
  await image.decode();
  return image;
}
function shuffleArtwork(choices,last){
  for(let i=choices.length-1;i>0;i--){
    const j=Math.floor(Math.random()*(i+1));
    [choices[i],choices[j]]=[choices[j],choices[i]];
  }
  if(choices.length>1&&choices.at(-1)===last){
    [choices[0],choices[choices.length-1]]=[choices.at(-1),choices[0]];
  }
  return choices;
}
async function chooseHero(){
  const ticket=++heroLoad;
  const heroes=await loadHeroes();
  if(ticket!==heroLoad||!heroes.length)return;
  if(!heroBag.length)heroBag=[...heroes].sort(()=>Math.random()-.5);
  if(heroBag.length>1&&heroBag.at(-1).id===currentHero?.id)[heroBag[0],heroBag[heroBag.length-1]]=[heroBag.at(-1),heroBag[0]];
  const hero=heroBag.pop();
  let gallery=[];
  if(window.pywebview?.api){
    try{
      if(!skinGallery)skinGallery=call('hero_gallery').catch(error=>{skinGallery=null;throw error;});
      gallery=await skinGallery;
    }catch{/* The bundled artwork is available without a skin library. */}
  }
  if(ticket!==heroLoad)return;
  const cycle=heroArtCycles.get(hero.id);
  const choices=cycle?.remaining.length?cycle.remaining:shuffleArtwork([hero,...gallery.filter(skin=>skin.hero===hero.id)],cycle?.last);
  let art=choices.at(-1),image;
  if(art!==hero){
    try{
      const src=await call('hero_artwork',art.id);
      if(ticket!==heroLoad)return;
      image=await decodeHeroArt(src);
    }catch{art=hero;}
  }
  if(ticket!==heroLoad)return;
  if(!image){
    try{
      const publicArt=globalThis.document?.documentElement?.dataset?.artwork==='public';
      image=await decodeHeroArt(publicArt?'assets/brand/divetrail-mark.svg':`assets/heroes/illustrations/${hero.id}.png`);
      if(publicArt)image.classList.add('fallback-art');
    }
    catch{image=await decodeHeroArt('assets/brand/divetrail-mark.svg');image.classList.add('fallback-art');}
  }
  if(ticket!==heroLoad)return;
  image.alt=hero.name;
  $('#hero-name').textContent=`${hero.id.replaceAll('-',' ').toUpperCase()} / ${hero.name}`;
  $('#hero-line').textContent=hero.line;
  $('#hero-panel').dataset.hero=hero.id;
  const skinLabel=$('#hero-skin');
  skinLabel.textContent=art===hero?'':art.name;
  skinLabel.title=art===hero?'':['透明立绘',art.attribution].filter(Boolean).join(' · ');
  fitHeroArt(image,art);
  $('#hero-art').replaceWith(image);
  heroArtCycles.set(hero.id,{remaining:choices.slice(0,-1).filter(choice=>choice!==art),last:art});
  currentHero=hero;
  currentArt=art;
}
function fitHeroArt(image=$('#hero-art'),art=currentArt){
  const {clientWidth:width,clientHeight:height}=$('#hero-art-frame');
  const {naturalWidth,naturalHeight}=image;
  if(!width||!height||!naturalWidth||!naturalHeight)return;
  const [left,top,right,bottom]=art?.bounds??[0,0,naturalWidth,naturalHeight];
  if(!(right>left&&bottom>top))return;
  const padX=Math.min(12,width*.04),padY=Math.min(12,height*.04);
  const scale=Math.min((width-2*padX)/(right-left),(height-padY)/(bottom-top));
  const focused=width/2-(art?.focus_x??(left+right)/2)*scale;
  const x=Math.max(padX-left*scale,Math.min(focused,width-padX-right*scale));
  image.style.width=naturalWidth*scale+'px';
  image.style.height=naturalHeight*scale+'px';
  image.style.left=x+'px';
  image.style.top=(height-bottom*scale)+'px';
}
function initHeroCard(){
  $('#hero-art').addEventListener('load',()=>fitHeroArt());
  if(window.ResizeObserver)new ResizeObserver(()=>fitHeroArt()).observe($('#hero-art-frame'));
  action(async()=>{
    const heroes=await loadHeroes();
    if(!currentArt){
      currentArt=currentHero=heroes.find(hero=>hero.id==='vantage');
      fitHeroArt();
    }
  });
  const start=()=>{if(!heroLoad)action(chooseHero);};
  if(window.pywebview?.api||!window.chrome?.webview)start();
  else window.addEventListener('pywebviewready',start,{once:true});
}
function render(){
  const c={...defaults,...state.config};state.config=c;applyTheme(c.theme);
  document.documentElement.classList.toggle('reduce-motion',!!c.reduce_motion);
  const overview=$('#overview-view'),wasHidden=overview.classList.contains('hide-hero-board');
  overview.classList.toggle('hide-hero-board',!c.show_hero_board);
  if(wasHidden&&c.show_hero_board&&view==='overview')fitHeroArt();
  $('#profile-name').textContent=c.self_name?displayName(c.self_name):'待设置';
  const unassigned=state.profile_isolation?.unassigned||0;
  $('#profile-notice').hidden=!!c.self_name&&!unassigned;
  $('#profile-notice').classList.toggle('profile-required',!c.self_name);
  $('#profile-notice-title').textContent=c.self_name?'旧记录待确认':'首次使用 · 请设置档案';
  $('#profile-notice-message').textContent=!c.self_name?'识别排位大厅中的游戏昵称和分数，或直接手动填写游戏 ID。':`有 ${unassigned} 局旧记录的归属待确认，暂未计入当前档案。`;
  $('#profile-setup').hidden=!!c.self_name;
  $('#season-caption').textContent=`${c.season} / ${String(c.split).padStart(2,'0')}`;
  renderCaptureStatus();
  renderRank();renderOverview();renderMatches();renderRecovery();renderProfile();
  if(view==='teammates')renderPlayerHeroes();
  if(view==='maps')MapView.render(state);
}
function captureTime(value,formatter=clock){
  if(value==null||value==='')return '';
  const moment=new Date(typeof value==='number'&&value<1e12?value*1000:value);
  return Number.isNaN(moment.getTime())?'':formatter(moment);
}
function matchClock(match){
  const start=captureTime(match.round_started_at,Number);
  const end=captureTime(match.round_end_at??match.settled_at,Number);
  if(!start||!end||end<start||pendingTime(match)||match.time_corrected)return captureTime(match.time)||'—';
  const [firstDay,firstTime]=localTimeInput(start).slice(5).split('T');
  const [lastDay,lastTime]=localTimeInput(end).slice(5).split('T');
  const day=firstDay.replace('-','/');
  const last=firstDay===lastDay?lastTime:`${lastDay.replace('-','/')} ${lastTime}`;
  return `${day} ${firstTime}–${last}`;
}
function recognitionStatus(work, completion){
  const title={queued:'截图已保存，等待识别',reading:'正在识别截图',awaiting_frame:'等待结算画面',saved:'截图已归档',review:'截图待核对'}[work?.stage];
  if(!title)return null;
  const page={summary:'总结',rank:'分数',settlement:'结算',manual:'手动截图',hud:'局内快照',lobby:'大厅',profile:'个人数据'}[work.page];
  const started=captureTime(work.round_started_at),captured=captureTime(work.captured_at);
  const count=Number(work.pending_count)||0;
  const verified=work.stage==='saved'&&completion?.round_id&&completion.round_id===work.round_id;
  const message=verified&&completion.complete?'整局已补齐':verified&&work.page==='rank'
    ?(completion.rp_confirmed?'RP 已核验':'RP 待核对')
    :verified&&completion.summary_saved&&work.page==='summary'?'总结已留存，可以翻页':title;
  const labelTime=captureTime(work.round_started_at??work.captured_at,value=>localTimeInput(value).slice(11));
  const detail=[labelTime?`${labelTime}场次`:'',page,count>0?`待识别 ${count}`:''].filter(Boolean);
  const tooltip=[message,started?'开局 '+captureTime(work.round_started_at,fullClock):'',captured?'截图 '+captureTime(work.captured_at,fullClock):'',page,count>0?`待识别 ${count}`:''].filter(Boolean);
  return {text:[message,...detail].join(' · '),title:tooltip.join(' · ')};
}
function renderCaptureStatus(){
  const r=state.runtime||{},configured=!!state.config.self_name,profileChange=!!r.profile_change;
  $('#record-control').textContent=!configured?'设置档案':state.running?'暂停记录':'继续记录';
  $('#capture-state').textContent=!configured?'等待设置档案':profileChange?'待核对档案':state.running?(r.location&&r.location!=='待识别'?r.location:'正在检测'):'已暂停';
  const progress=configured&&!profileChange?recognitionStatus(r.recognition,r.capture_completion):null;
  const meter=$('#recognition-progress'),step={queued:1,reading:2}[r.recognition?.stage];
  meter.hidden=!progress||!step;
  if(step){meter.value=step;meter.setAttribute('aria-valuetext',progress?.text||'');}
  const message=(!configured?'确认游戏昵称后开始记录':profileChange?'确认昵称后恢复采集':r.error?r.message||'采集待检查':progress?.text||r.hotkey_error||r.stage||'等待 Apex')+(!progress&&state.record_audit?.state==='checking'?' · 核对24h':'');
  const status=$('#capture-message');
  if(status.textContent!==message)status.textContent=message;
  status.title=r.error?message:progress?.title||message;
  status.hidden=configured&&!profileChange&&!r.error&&!progress&&!r.hotkey_error
    &&state.record_audit?.state!=='checking'&&!!r.location&&message===r.location;
  $('#capture-dot').className=profileChange||r.error||r.hotkey_error?'error':configured&&state.running?'running':'';
  renderProfileSetup(r.profile_scan);
  renderProfileChange(r.profile_change);
}
function renderRankValue(id,value,format=fmt){
  const element=$('#'+id);
  if(element.metricTarget===value)return;
  if(Number.isFinite(element.metricTarget)&&Number.isFinite(value)){
    animateMetric(element,value,format);
  }else{
    element.metricTarget=value;
    element.textContent=format(value);
  }
}
function renderRank(){
  const current=state.rank_progress?.current;
  const latest=state.matches.find(m=>(m.mode||'ranked')==='ranked'&&validDelta(m));
  const known=Number.isFinite(current?.progress)&&Number.isFinite(current?.capacity)&&current.capacity>0;
  $('#rank-name').textContent=current?.rank||'—';
  renderRankValue('rank-progress',current?.progress??null,value=>fmt(value,0));
  $('#rank-capacity').textContent=fmt(current?.capacity,0);
  const percent=known?Math.max(0,Math.min(100,current.progress/current.capacity*100)):0;
  const track=$('#rank-track');
  track.setAttribute('aria-valuenow',String(Math.round(percent)));
  track.setAttribute('aria-valuetext',known?`${fmt(current.progress)} / ${fmt(current.capacity)} RP`:'等待段位采集');
  track.classList.toggle('reduce-motion',!!state.config.reduce_motion);
  $('#progress-fill').style.width=percent+'%';
  const tiers=[['钻石','diamond'],['白金','platinum'],['铂金','platinum'],['黄金','gold'],['白银','silver'],['青铜','bronze'],['大师','master'],['猎杀','apexpredator']];
  const key=tiers.find(([name])=>current?.rank?.includes(name))?.[1]||'rookie',div=current?.rank?.match(/\b(IV|III|II|I)\b/)?.[1]||'I';
  const icon=$('#rank-icon'),path=`assets/ranks/${key}-${({I:1,II:2,III:3,IV:4})[div]}.png`;
  if(document.documentElement.dataset.artwork!=='public'&&icon.dataset.requested!==path){
    icon.dataset.requested=path;
    icon.src=path;
  }
  icon.alt=current?.rank||'段位待采集';
  icon.parentElement.dataset.tier=key;
  $('#rank-source').textContent=current?(current.estimated?'缓存推算':({lobby:'大厅',manual:'手动校准',settlement:'结算',upload:'补录',initial:'缓存'})[current.source]||'缓存'):'待采集';
  const cachedAt=current?.cache?.captured_at;
  const rpOrigin=current?.estimated?'按已记录的排位数据推算':current?'由游戏段位及分数换算':'等待采集排位大厅或经验值';
  $('#rank-cache-time').textContent=cachedAt?`${rpOrigin} · ${current.source==='upload'?'导入':current.estimated||current.cache.historical?'缓存':'更新'} ${clock(cachedAt)}`:rpOrigin;
  renderRankValue('total-rp-value',state.rank_progress?.total_rp??current?.total_rp??null,value=>fmt(value,0));
  renderRankValue('latest-delta-value',latest?.rank?.delta??null,value=>signed(value==null?null:Math.round(value)));
  $('#latest-delta').className=tone(latest?.rank?.delta);
}
$('#rank-icon').addEventListener('error',event=>{
  const icon=event.currentTarget;
  const fallback='assets/brand/rank-fallback.svg';
  if(icon.getAttribute('src')!==fallback)icon.src=fallback;
});
let overviewRange='12';
try{const saved=localStorage.getItem('overview-range')||localStorage.getItem('metrics-hours');if(['12','24','30','all'].includes(saved))overviewRange=saved;}catch{}
function recentMatches(now,hours){
  const start=now-hours*3600000;
  return state.matches.filter(m=>!pendingTime(m)&&statusCode(m)!=='ongoing'&&m.created*1000>=start&&m.created*1000<=now);
}
function overviewScope(now=Date.now()){
  const scope=MatchLogOverview.range(state.matches,overviewRange,now);
  const ranked=scope.rows.filter(m=>(m.mode||'ranked')==='ranked');
  const deltas=ranked.filter(m=>effectiveRp(m)!=null);
  let best=null;
  for(const match of scope.rows){
    const player=own(match),damage=player?.damage;
    if(Number.isFinite(damage)&&(!best||damage>best.damage)){
      best={damage,placement:match.summary?.placement,hero:player.hero,kills:player.kills,assists:player.assists,knocks:player.knocks};
    }
  }
  return {...scope, now, scored:deltas.length, missing:ranked.length-deltas.length, snapshots:deltas.filter(m=>!validDelta(m)).length, best,
    net:deltas.length?deltas.reduce((sum,m)=>sum+effectiveRp(m),0):null};
}
function renderOverview(){
  const scope=overviewScope();
  renderMetrics(scope);renderChart(scope);renderFriends(scope);renderPerformance(scope);
  $('#recent-count').textContent=`/ ${String(scope.rows.length).padStart(2,'0')}`;
  $('#recent-table').innerHTML=table(scope.rows.slice(0,10));
}
function renderPerformance(scope){
  const data=MatchLogOverview.insights(scope.rows,own,effectiveRp);
  const best=scope.best;
  const line=(title,value,detail)=>`<article class="panel insight-card"><h3>${title}</h3><strong>${value}</strong><p>${detail}</p></article>`;
  $('#performance-insights').innerHTML=scope.rows.length?`<div class="section-title"><h2>表现细分</h2><span>${esc(scope.label)}</span></div><div class="insight-grid">
    ${line('最高伤害',fmt(best?.damage),best?esc(heroName(best.hero))+(best.placement?` · 第 ${best.placement} 名`:''):'暂无伤害')}
    ${line('千伤局',`${data.highDamage} <small>/ ${data.damageGames} 局</small>`,data.median==null?'暂无伤害':`伤害中位数 ${fmt(data.median)}`)}
    ${line('排位盈亏',`<span class="positive">${data.up}</span> <small>上分</small> · <span class="negative">${data.down}</span> <small>掉分</small>`,`${data.scored} 局有 RP`)}
    ${line('常用阵容',data.roster?esc(data.roster.names.join(' · ')):'—',data.roster?`同阵容 ${data.roster.count} 局`:'等待三人英雄齐全')}
  </div>`:'';
}

function animateMetric(element,value,format){
  if(!Number.isFinite(value))return;
  if(state.config.reduce_motion||matchMedia('(prefers-reduced-motion: reduce)').matches){element.metricTarget=value;element.textContent=format(value);return;}
  const from=element.metricTarget??value;
  if(from===value)return;
  element.metricTarget=value;
  const started=performance.now();
  const frame=now=>{
    if(!element.isConnected||element.metricTarget!==value)return;
    const t=Math.min(1,(now-started)/250);
    element.textContent=format(from+(value-from)*(1-(1-t)**3));
    if(t<1)requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}
function renderMetrics(scope=overviewScope()){
  const {rows:daily,start,now,net}=scope,personal=daily.map(own).filter(Boolean);
  const rpNote=`${scope.scored} 局已记分`;
  const kills=personal.filter(p=>p.kills!=null),assists=personal.filter(p=>p.assists!=null);
  const placed=daily.filter(m=>m.summary?.placement!=null),wins=placed.filter(m=>m.summary.placement===1).length,damage=personal.filter(p=>p.damage!=null);
  const counts={recorded:0,incomplete:0,snapshot:0};
  for(const match of daily)counts[statusCode(match)]++;
  const note=`${counts.snapshot} 局来自最后快照`;
  const items=[['已记对局',daily.length,'局',note],['净变化',signed(net),'RP',rpNote,tone(net)],['场均伤害',fmt(avg(damage.map(p=>p.damage))),'',`${damage.length} 局`],['平均排名',fmt(avg(placed.map(m=>m.summary.placement))),'名',`吃鸡 ${winCount(wins,placed.length)}`],['场均击杀',fmt(avg(kills.map(p=>p.kills)),2),'',`场均助攻 ${fmt(avg(assists.map(p=>p.assists)),2)} · ${kills.length} 局`]];
  $('#metrics-title').textContent=`${scope.label}表现`;
  $('#metrics-caption').textContent=`${clock(start)} — ${clock(now)}`;
  const signature=JSON.stringify(items);
  if(renderMetrics.signature===signature)return;
  renderMetrics.signature=signature;
  const previous=renderMetrics.values||[];
  $('#metrics').innerHTML=items.map(([label,value,unit,note,color],i)=>`<div class="metric ${i===1?'focus':''}" ><div class="metric-label">${label}</div><div class="metric-value ${color||''}"><span>${value}</span><small>${unit}</small></div><div class="metric-note">${note}</div></div>`).join('');
  const values=[daily.length,net,avg(damage.map(p=>p.damage)),avg(placed.map(m=>m.summary.placement)),avg(kills.map(p=>p.kills))];
  $$('#metrics .metric-value > span').forEach((element,index)=>{
    element.metricTarget=previous[index];
    animateMetric(element,values[index],index===1?x=>signed(Math.round(x)):x=>fmt(x,index===4?2:index===0?0:1));
  });
  renderMetrics.values=values;
}
const overviewButtons=$$('[data-overview-range]');
function markOverviewRange(){
  for(const button of overviewButtons)button.setAttribute('aria-pressed',String(button.dataset.overviewRange===overviewRange));
}
markOverviewRange();
for(const button of overviewButtons)button.onclick=()=>{
  overviewRange=button.dataset.overviewRange;
  markOverviewRange();
  try{localStorage.setItem('overview-range',overviewRange);}catch{}
  renderOverview();
};
function rpDetail(rank){
  if(!rank)return '';
  const source={anchors:'进度差额复核',breakdown:'游戏明细复核',pending:'计分动画待核对'}[rank.delta_source];
  const protection=rank.demotion_protection;
  const shield=protection?`降级保护${protection.remaining!=null?` · 剩余 ${protection.remaining}/${protection.capacity}`:''}${protection.refund!=null?` · 返还 ${fmt(protection.refund)} RP`:''}`:'';
  const verification=rank.validation;
  const conflict=verification?.state==='conflict'?['RP 校验有冲突',
    verification.tally!=null?`明细合计 ${signed(verification.tally)} RP`:'',
    verification.rule_value!=null?`规则验算 ${signed(verification.rule_value)} RP`:'',
    verification.entry_cost!=null?`${verification.entry_source==='opening'?'开局门票':'结算门票'} ${fmt(verification.entry_cost)} RP`:'',
  ].filter(Boolean).join(' · '):'';
  const parts=[shield,source,rank.read_delta!=null?`截图原读 ${signed(rank.read_delta)} RP`:'',
    rank.delta==null&&rank.rule_estimate?`规则估算 ${signed(rank.rule_estimate.value)} RP${rank.rule_estimate.fee_source==='tier'?' · 按段位门票':''}`:'',conflict].filter(Boolean);
  return parts.length?`<div class="detail-note">${esc(parts.join(' · '))}</div>`:'';
}
function resizeChartText(){const svg=$('#chart svg');if(!svg)return;const scale=svg.clientWidth/570;if(!scale)return;$('#chart').style.setProperty('--chart-font',14/scale+'px');$('#chart').style.setProperty('--chart-endpoint-font',13/scale+'px');}
function renderChart(scope=overviewScope()){
  const width=$('#chart').clientWidth||400,graphPx=innerHeight<=750?108:Math.max(122,Math.min(185,Math.round(width*.29))),height=Math.round(graphPx*570/width),bottom=height-29,plotHeight=bottom-15;
  const selected=MatchLogOverview.points(state.climb_points||state.rank_progress?.points||[],scope,state.config.split);
  const signature=JSON.stringify([selected,overviewRange,state.config.theme,width,graphPx]);if(renderChart.signature===signature)return;renderChart.signature=signature;
  const points=selected.map((p,i)=>({...p,i})),valid=points.filter(p=>p.value!=null);
  const count=points.filter(point=>point.match_id).length;
  const caption=[scope.label,scope.label!=='近 30 局'||count!==30?`${count} 局`:'',valid.length?'虚线为待补分或校准区间':''].filter(Boolean).join(' · ');
  $('#chart-caption').textContent=caption;
  $('#chart-caption').title=caption;
  if(!valid.length){
    $('#chart').innerHTML=`<div class="chart-empty" style="height:${graphPx}px">${scope.rows.length?'这个范围内暂无可用 RP':'这个范围内暂无对局'}</div><div class="chart-tooltip chart-tooltip-spacer" aria-hidden="true"></div>`;
    return;
  }
  const values=valid.map(p=>p.value),low=Math.min(...values),high=Math.max(...values),pad=Math.max(8,(high-low)*.035),min=low-pad,max=high+pad,span=max-min;
  const digits=Math.max(String(Math.round(min)).length,String(Math.round(max)).length);
  const left=Math.max(62,Math.ceil((digits*8+8)*570/width));
  const x=p=>left+p.i/Math.max(points.length-1,1)*(552-left),y=p=>bottom-(p.value-min)/span*plotHeight;
  const plotted=valid.map(p=>({...p,x:x(p),y:y(p)}));
  const color=(p,previous)=>p.adjustment?'--gold':p.value<(previous?.value??p.value)?'--red':p.value>(previous?.value??p.value)?'--green':'--muted';
  const lines=ClimbChart.segments(plotted).map(({from:a,to:b,path})=>`<path d="${path}" fill="none" stroke="var(${color(b,a)})" stroke-width="2.1" ${b.i-a.i>1||b.adjustment||a.split!==b.split?'stroke-dasharray="4 4"':''}/>`).join('');
  const tip=p=>[p.label||'对局结算',p.at&&!p.time_pending?fullClock(p.at*1000):'时间待确认',`${fmt(p.value)} RP`,p.delta!=null?`${signed(p.delta)} RP`:'',p.adjustment?`校准 ${signed(p.adjustment)} RP`:''].filter(Boolean).join(' · ');
  const ticks=[];for(const p of plotted){if(!p.at||p.time_pending)continue;const label=dateKey(p.at*1000).slice(5).replace('-','/');if(!ticks.some(t=>t.label===label))ticks.push({label,x:p.x});}
  const compactTicks=ticks.length>4?[ticks[0],ticks[Math.floor(ticks.length/2)],ticks.at(-1)]:ticks,shown=[];
  for(const tick of compactTicks)if(!shown.length||tick.x-shown.at(-1).x>=Math.max(58,48*570/width))shown.push(tick);
  const radius=plotted.length>50?2.5:plotted.length>20?3.2:4.2;
  const latestPoint=plotted.at(-1),latestKey=JSON.stringify([latestPoint.match_id,latestPoint.at,latestPoint.value]);
  const animate=renderChart.latestKey&&renderChart.latestKey!==latestKey&&!state.config.reduce_motion;
  renderChart.latestKey=latestKey;
  $('#chart').innerHTML=`<svg class="chart-svg" viewBox="0 0 570 ${height}" style="height:${graphPx}px" role="img" aria-label="上分轨迹">${[0,.5,1].map(f=>`<line x1="${left}" x2="552" y1="${15+plotHeight*f}" y2="${15+plotHeight*f}" stroke="var(--line)" stroke-dasharray="3 5"/><text x="0" y="${19+plotHeight*f}">${Math.round(max-span*f)}</text>`).join('')}${lines}${plotted.map((p,i)=>`<circle class="chart-point${animate&&i===plotted.length-1?' fresh-point':''}" tabindex="0" data-tooltip="${esc(tip(p))}" aria-label="${esc(tip(p))}" cx="${p.x}" cy="${p.y}" r="${radius}" fill="var(${color(p,plotted[i-1])})" stroke="var(--panel)" stroke-width="2"/>`).join('')}${shown.map(t=>`<text class="chart-endpoint" x="${t.x}" y="${height-5}" text-anchor="${t.x>500?'end':'start'}">${t.label}</text>`).join('')}</svg><div class="chart-tooltip" role="status" title="${esc(tip(latestPoint))}">${esc(tip(latestPoint))}</div>`;
  if(animate)$('#chart svg')?.classList.add('chart-updated');
  resizeChartText();
}

function friendRow(r){return `<div class="friend-row"><div class="friend-avatar">${esc(displayName(r.name).slice(0,1).toUpperCase())}</div><div><div class="friend-name">${esc(displayName(r.name))}</div><div class="friend-meta">${r.games?`${r.games} 局同队 · 场均 ${fmt(r.damage)} 伤害`:'等你们一起开场'}</div></div><div class="friend-rp ${tone(r.rp_games?r.my_rp:null)}">${r.rp_games?signed(r.my_rp):'—'}<small>你的同队净上分</small></div></div>`;}
function friendOverview(r){
  const name=esc(displayName(r.name));
  const delta=r.rp_games?r.my_rp:null;
  const initial=esc(Array.from(displayName(r.name))[0]?.toUpperCase());
  return `<section class="friend-overview"><header><span class="friend-avatar">${initial}</span><strong class="friend-name" title="${name}">${name}</strong><span class="friend-games">${r.games} 局同队</span></header>
    <dl class="friend-numbers">
      <div><dt>场均击杀</dt><dd>${fmt(r.kills,2)}</dd></div>
      <div><dt>场均助攻</dt><dd>${fmt(r.assists,2)}</dd></div>
      <div><dt>场均伤害</dt><dd>${fmt(r.damage)}</dd></div>
      <div title="和该队友同场时，你获得的净 RP"><dt>你的同队净 RP</dt><dd class="${tone(delta)}">${signed(delta)}</dd></div>
    </dl></section>`;
}
function recentFriendStats(now=Date.now(), matches=recentMatches(now,12)){
  const self=norm(state.config.self_name);
  const friends=new Map();
  for(const name of state.config.friends){
    const key=norm(name);
    if(key&&key!==self&&!friends.has(key))friends.set(key,displayName(name));
  }
  return Array.from(friends,([key,name])=>{
    const players=matches.map(m=>m.summary?.players?.find(p=>norm(p.name)===key)).filter(Boolean);
    return {name,values:['kills','assists','damage'].map(field=>avg(players.map(p=>p[field]).filter(Number.isFinite)))};
  });
}
function renderRecentFriends(scope=overviewScope()){
  const rows=recentFriendStats(scope.now,scope.rows);
  $('#friends-recent-title').textContent=`固定队友 · ${scope.label}`;
  $('#friends-recent-content').innerHTML=rows.length?
    `<table><thead><tr><th scope="col">队友</th><th scope="col">场均击杀</th><th scope="col">场均助攻</th><th scope="col">场均伤害</th><th scope="col">近期伤害</th></tr></thead><tbody>${rows.map(({name,values})=>`<tr><th scope="row">${esc(name)}</th>${values.map((value,i)=>`<td>${fmt(value,i===2?1:2)}</td>`).join('')}<td>${friendDamageTrend(scope.rows,name)}</td></tr>`).join('')}</tbody></table>`:
    '<div class="empty-state">在设置里添加固定队友。</div>';
}
function friendDamageTrend(matches,name){
  const key=norm(name);
  const damage=matches.map(m=>({at:m.created,value:m.summary?.players?.find(p=>norm(p.name)===key)?.damage}))
    .filter(({at,value})=>Number.isFinite(at)&&Number.isFinite(value)&&value>=0)
    .sort((a,b)=>a.at-b.at).slice(-8).map(({value})=>value);
  if(damage.length<2)return '—';
  const low=Math.min(...damage),high=Math.max(...damage);
  const points=damage.map((value,i)=>`${(2+i*88/(damage.length-1)).toFixed(1)},${(high===low?11:20-18*(value-low)/(high-low)).toFixed(1)}`).join(' ');
  const label=`近 ${damage.length} 局伤害，按时间顺序 ${damage.join('、')}`;
  return `<svg class="friend-damage-trend" width="92" height="22" viewBox="0 0 92 22" role="img" aria-label="${label}"><title>${label}</title><polyline points="${points}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
function scopedFriends(scope){
  return state.config.friends.filter(Boolean).map(name=>{
    const matches=scope.rows.filter(m=>m.summary?.players?.some(p=>norm(p.name)===norm(name)));
    const rows=matches.flatMap(m=>m.summary.players.filter(p=>norm(p.name)===norm(name)));
    const ranked=matches.filter(m=>m.mode==='ranked'&&validDelta(m));
    const average=field=>avg(rows.map(p=>p[field]).filter(Number.isFinite));
    return {name,games:matches.length,damage:average('damage'),kills:average('kills'),assists:average('assists'),
      rp_games:ranked.length,my_rp:ranked.reduce((sum,m)=>sum+m.rank.delta,0)};
  });
}
function renderFriends(scope=overviewScope()){
  const fixed=(state.teammates||[]).filter(r=>state.config.friends.some(n=>norm(n)===norm(r.name)));
  const profiles=new Map((state.player_hero_stats||[]).map(player=>[player.key,player]));
  $('#friends-list').innerHTML=scopedFriends(scope).map(friendOverview).join('')||'<div class="empty-state">在设置里添加固定队友。</div>';
  if($('#friends-recent-dialog').open)renderRecentFriends(scope);
  $('#teammate-grid').innerHTML=fixed.map(r=>{
    const key=norm(r.name),profile=profiles.get(key);
    const metrics=[['共同对局',fmt(r.games)],['吃鸡场数',winCount(r.wins,profile?.placement_games)],['场均伤害',fmt(r.damage)],['场均击杀',fmt(r.kills)],['场均助攻',fmt(r.assists)],['场均击倒',fmt(r.knocks)]];
    return `<article class="panel teammate-card">${friendRow(r)}<div class="teammate-metrics">${metrics.map(([name,value])=>`<div><small>${name}</small><strong>${value}</strong></div>`).join('')}</div><button class="text-button teammate-heroes-link" data-player-heroes="${esc(key)}">查看英雄使用 ↗</button></article>`;
  }).join('')||'<div class="panel empty-state">添加固定队友，留下共同战绩。</div>';
}
function renderPlayerPerformance(player){
  const panel=$('#player-performance');
  if(!player?.games){panel.innerHTML='';return;}
  const fields=player.performance||{};
  const sampled=field=>fields[field]?.count?('基于 '+fields[field].count+' 局'):'尚未记录';
  const cards=[
    ['场均伤害',fmt(fields.damage?.average),sampled('damage')],
    ['最高伤害',fmt(fields.damage?.best),fields.damage?.count?player.thousand_damage+' 场千伤':'尚未记录'],
    ['场均击杀 / 助攻 / 击倒',['kills','assists','knocks'].map(key=>fmt(fields[key]?.average)).join(' / '),'最高击杀 '+fmt(fields.kills?.best,0)],
    ['平均排名',fmt(player.average_placement),player.placement_games?('前五 '+player.top_five+' 场 · 吃鸡 '+player.wins+' 场'):'尚未记录排名'],
    [player.is_self?'排位净 RP':'同队时我的净 RP',player.rp_games?signed(player.net_rp):'—',player.rp_games?(player.rp_games+' 局结算 · '+player.positive_rp+' 局上分'):'尚无正式计分',player.rp_games?tone(player.net_rp):''],
    ['最常用英雄',player.heroes[0]?.name||'—',player.heroes[0]?player.heroes[0].games+' 局 · 共使用 '+player.heroes.length+' 位':'尚未识别英雄'],
  ];
  panel.innerHTML='<div class="player-performance-grid">'+cards.map(([title,value,detail,accent=''])=>
    `<article class="player-performance-card"><h3>${esc(title)}</h3><strong class="${accent}">${esc(value)}</strong><small>${esc(detail)}</small></article>`).join('')+'</div>';
}

function renderPlayerHeroes(){
  const players=state.player_hero_stats||[];
  const player=players.find(p=>p.key===selectedPlayer)||players[0];
  selectedPlayer=player?.key||'';
  const signature=JSON.stringify([players,selectedPlayer,state.config.season,state.config.split]);
  if(renderPlayerHeroes.signature===signature)return;
  renderPlayerHeroes.signature=signature;
  const selector=$('#hero-player');
  selector.innerHTML=players.map(p=>`<option value="${esc(p.key)}">${p.is_self?'自己 · ':''}${esc(p.name)}</option>`).join('');
  selector.disabled=!player;
  selector.value=selectedPlayer;
  $('#hero-stats-scope').textContent=`${state.config.season} / ${String(state.config.split).padStart(2,'0')} · ${player?.is_self?'本人对局':'同队对局'}`;
  renderPlayerPerformance(player);
  const summary=$('#hero-player-summary'),detail=$('#player-hero-table');
  summary.textContent=player?`${fmt(player.games,0)} 局 · ${fmt(player.known_hero_games,0)} 局已识别英雄 · ${fmt(player.unknown_hero_games,0)} 局英雄待补 · 吃鸡 ${winCount(player.wins,player.placement_games)}`:'';
  if(!player?.heroes.length){
    detail.innerHTML=`<div class="empty-state">${player?.games?'有对局，英雄仍待识别。可在对局详情补填。':player?'当前赛段还没有该玩家的对局。':'填写游戏昵称后查看英雄统计。'}</div>`;
    return;
  }
  const cell=(value,count)=>`<td>${fmt(value)}<small>${fmt(count,0)} 局有数据</small></td>`;
  detail.innerHTML=`<table><thead><tr><th scope="col">英雄</th><th scope="col">使用局数</th><th scope="col">吃鸡场数</th><th scope="col">平均排名</th><th scope="col">场均伤害</th><th scope="col">场均击杀</th><th scope="col">场均助攻</th><th scope="col">场均击倒</th><th scope="col">对局明细</th></tr></thead><tbody>${player.heroes.map(hero=>`<tr><th scope="row">${esc(hero.name)}</th><td>${fmt(hero.games,0)} 局</td><td>${winCount(hero.wins,hero.placement_games)}<small>${fmt(hero.placement_games,0)} 局有排名</small></td>${cell(hero.average_placement,hero.placement_games)}${cell(hero.damage,hero.damage_games)}${cell(hero.kills,hero.kills_games)}${cell(hero.assists,hero.assists_games)}${cell(hero.knocks,hero.knocks_games)}<td><button type="button" class="text-button" data-hero-matches="${esc(hero.name)}" aria-label="查看 ${esc(hero.name)} 的对局">查看对局 ↗</button></td></tr>`).join('')}</tbody></table>`;
}
function table(matches){
  if(!matches.length)return '<div class="empty-state">这里还没有对局。</div>';
  const rows=matches.map(m=>{
    const p=own(m),delta=effectiveRp(m),time=matchClock(m);
    const teammates=m.summary?.players?.filter(player=>norm(player.name)!==norm(state.config.self_name))||[];
    const roster=(p?[p,...teammates]:teammates).map(player=>{
      const hero=esc(heroName(player.hero)),name=esc(player===p?'本人':(displayName(player.name)||'—'));
      const id=portraitIds.get(heroName(player.hero));
      const image=id&&document.documentElement.dataset.artwork!=='public'?`assets/heroes/references/${id}/roster.png`:null;
      return image?`<span class="row-portrait fallback" title="${name} · ${hero}"><span>${esc(displayName(player.name).slice(0,1)||'—')}</span><img src="${image}" alt=""></span>`:
        `<span class="row-portrait fallback" title="${name}">${esc(displayName(player.name).slice(0,1)||'—')}</span>`;
    }).join('');
    return `<tr data-match="${esc(m.id)}" role="button" tabindex="0" aria-label="对局 ${esc(time)}">
      <td class="time-cell">${esc(time)}<span class="match-mode">${modeName(m.mode)}</span></td>
      <td class="map-cell">${esc(MapView.label(m.map_name))}</td>
      <td><span class="place ${placeClass(m.summary?.placement)}">${m.summary?.placement!=null?'#'+m.summary.placement:'—'}</span></td>
      <td class="combat-cell">${p?`${fmt(p.kills)} / ${fmt(p.assists)} / ${fmt(p.knocks)}`:'—'}</td><td>${fmt(p?.damage)}</td>
      <td class="delta-cell ${tone(delta)}">${matchRp(m)}${m.rank?.demotion_protection?'<small class="rp-protection">降级保护</small>':''}</td>
      <td class="table-name">${roster?`<div class="row-portraits">${roster}</div>`:'—'}</td><td>${matchStatus(m)}</td></tr>`;
  });
  return `<table><thead><tr><th>时间</th><th>地图</th><th>排名</th><th>击杀 / 助攻 / 击倒</th><th>伤害</th><th>本局 RP</th><th>阵容</th><th>状态</th></tr></thead><tbody>${rows.join('')}</tbody></table>`;
}
function matchBoundary(id){
  return MatchLogFilters.boundaryInput($(`#match-${id}`).value,$(`#match-${id}-hour`).value,$(`#match-${id}-minute`).value,id==='end');
}
function matchSelection(){
  const search=norm($('#match-search').value),mode=$('#mode-filter').value||'all',status=$('#status-filter').value||'all';
  const range=MatchLogFilters.timeRange(matchBoundary('start'),matchBoundary('end'));
  const player=heroMatchFilter&&(state.player_hero_stats||[]).find(p=>p.key===heroMatchFilter.key);
  const hero=player?.heroes.find(h=>h.name===heroMatchFilter.name);
  const heroIds=heroMatchFilter?new Set(hero?.match_ids||[]):null;
  const active=!!(search||mode!=='all'||filter!=='all'||status!=='all'||range.active||heroMatchFilter);
  const matches=range.valid?state.matches.filter(m=>
    (!heroIds||heroIds.has(m.id))&&(mode==='all'||(m.mode||'ranked')===mode)&&
    (filter==='all'||m.summary?.placement===1)&&matchesStatus(m,status)&&
    (!search||m.summary?.players?.some(p=>norm(p.name).includes(search)))&&MatchLogFilters.matchesTimeRange(m,range)):[];
  return {matches,active,range,player};
}
function renderMatches(){
  const {matches,active,range,player}=matchSelection();
  const message=range.error==='invalid_date'?'请选择有效的日期和时间。':range.error==='reversed'?'结束时间早于开始时间。':'';
  $('#matches-table').innerHTML=message?`<div class="empty-state">${message}</div>`:matches.length||!active?table(matches):
    '<div class="empty-state">没有符合筛选条件的对局。 <button type="button" class="text-button" data-clear-match-filters>清除筛选</button></div>';
  const timePreset=$('#custom-time [data-match-hours][aria-pressed=true]');
  $('#match-time-label').textContent=timePreset?.textContent||(range.active?'自定义日期':'全部时间');
  $('#match-time-clear').disabled=!range.active;
  const statusFilter=$('#status-filter');
  statusFilter.querySelector('[value=ongoing]').hidden=statusFilter.value!=='ongoing'&&!state.matches.some(m=>statusCode(m)==='ongoing');
  const chip=$('#hero-match-filter');
  chip.hidden=!heroMatchFilter;
  $('#hero-match-filter-label').textContent=heroMatchFilter?`${player?.name||heroMatchFilter.label} · ${heroMatchFilter.name}`:'';
  const exportButton=$('#export-btn');
  exportButton.disabled=!matches.length;
  exportButton.textContent=`导出 · ${matches.length} 局`;
  $('#merge-matches-btn').disabled=state.matches.length<2;
  $('#undo-merge-btn').hidden=!state.can_undo_merge;
}

let shareRender=0,shareData=null,shareImage=null,shareSkins=[];
const preferredShareSkins=new Map();
function drawShareCard(){
  if(!shareData)return;
  const {scope,owner,trajectory}=shareData;
  MatchLogOverview.share($('#share-card'),scope,owner,$('#share-hide-names').checked,trajectory,shareImage);
}
function shareBusy(busy){
  $('#share-save-btn').disabled=$('#share-copy-btn').disabled=busy;
}
async function showShareSkin(value){
  const ticket=++shareRender,hero=shareData?.hero;
  if(!hero){shareBusy(false);return;}
  const skin=shareSkins.find(item=>item.id===value);
  shareBusy(true);
  try{
    const original=`assets/heroes/illustrations/${hero}.png`;
    let image;
    try{
      image=await decodeHeroArt(skin?await call('hero_artwork',skin.id):original);
    }catch{
      if(skin){
        try{image=await decodeHeroArt(original);}catch{image=null;}
      }
      if(ticket===shareRender){
        $('#share-skin').value='';
        preferredShareSkins.set(hero,'');
        if(skin)toast(image?'皮肤暂时无法读取，已显示默认立绘':'皮肤暂时无法读取');
      }
    }
    if(ticket!==shareRender)return;
    const selected=$('#share-skin').value;
    shareImage=image?{image,bounds:selected?skin?.bounds:null}:null;
    preferredShareSkins.set(hero,selected);
  }finally{
    if(ticket===shareRender){drawShareCard();shareBusy(false);}
  }
}
async function openShareOverview(){
  const ticket=++shareRender,scope=overviewScope();
  const placements=scope.rows.map(match=>match.summary?.placement).filter(n=>Number.isInteger(n)&&n>0);
  scope.shareStats={placementGames:placements.length,wins:placements.filter(n=>n===1).length,
    topFive:placements.filter(n=>n<=5).length,damage:avg(scope.rows.map(match=>own(match)?.damage).filter(Number.isFinite))};
  shareData={scope,owner:displayName(state.config.self_name),hero:portraitIds.get(heroName(scope.best?.hero)),
    trajectory:MatchLogOverview.trajectory(scope.rows,effectiveRp)};
  shareImage=null;
  shareSkins=[];
  const select=$('#share-skin');
  select.replaceChildren(new Option('默认立绘',''));
  select.disabled=true;
  drawShareCard();
  $('#share-overview-dialog').showModal();
  shareBusy(!!scope.best?.hero);
  await loadHeroes().catch(()=>{});
  if(ticket!==shareRender)return;
  const hero=shareData.hero??portraitIds.get(heroName(scope.best?.hero));
  shareData.hero=hero;
  if(!hero){shareBusy(false);return;}
  if(!skinGallery)skinGallery=call('hero_gallery').catch(error=>{skinGallery=null;throw error;});
  const gallery=await skinGallery.catch(()=>[]);
  if(ticket!==shareRender)return;
  shareSkins=gallery.filter(skin=>skin.hero===hero);
  for(const skin of shareSkins)select.add(new Option(skin.name,skin.id));
  select.disabled=!shareSkins.length;
  const preferred=preferredShareSkins.get(hero)??(currentHero?.id===hero?currentArt?.id:null);
  select.value=preferred===''?'':shareSkins.find(skin=>skin.id===preferred)?.id??shareSkins[0]?.id??'';
  await showShareSkin(select.value);
}
$('#share-overview-btn').onclick=()=>action(openShareOverview);
$('#share-hide-names').onchange=drawShareCard;
$('#share-skin').onchange=event=>action(()=>showShareSkin(event.target.value));
$('#share-overview-dialog').addEventListener('close',()=>{shareRender++;});
async function saveShareCard(){
  const encoded=$('#share-card').toDataURL('image/png').split(',')[1];
  if((await call('save_share_card',encoded)).ok)toast('战绩图片已保存');
}
$('#share-save-btn').onclick=()=>action(saveShareCard);
$('#share-copy-btn').onclick=()=>action(async()=>{
  try{
    const blob=await new Promise(resolve=>$('#share-card').toBlob(resolve,'image/png'));
    await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);
    toast('战绩图片已复制');
  }catch{
    toast('剪贴板不可用，选择图片保存位置');
    await saveShareCard();
  }
});
function mergeRow(match){
  const placement=match.summary?.placement,player=own(match);
  const pages=[match.summary?'总结':'',match.rank?.delta!=null?'分数':''].filter(Boolean);
  return {
    label:`${clock(match.time)} · ${modeName(match.mode)} · ${placement!=null?'#'+placement:'排名待补'} · ${player?.damage!=null?'伤害 '+fmt(player.damage):'伤害待补'} · ${match.rank?.delta!=null?signed(match.rank.delta)+' RP':'RP 待补'}`,
    pages:pages.join(' / ')||statusLabel(match),
  };
}
function mergeError(message=''){
  const error=$('#merge-error');
  error.textContent=message;
  error.hidden=!message;
}
function renderMergeCandidates(){
  const first=$('#merge-first'),second=$('#merge-second'),selected=second.value;
  second.innerHTML='<option value="">请选择</option>'+state.matches
    .filter(match=>match.id!==first.value)
    .map(match=>`<option value="${esc(match.id)}">${esc(mergeRow(match).label)}</option>`).join('');
  if(state.matches.some(match=>match.id===selected&&match.id!==first.value))second.value=selected;
}
function resetMergePreview(){
  mergeRevision++;
  mergeDraft=null;
  mergeBusy=null;
  $('#merge-preview').hidden=true;
  $('#merge-preview').innerHTML='';
  $('#merge-selection').hidden=false;
  $('#merge-edit-selection').hidden=true;
  updateMergeControls();
  mergeError();
}
function updateMergeControls(){
  const submit=$('#merge-submit'),busy=!!mergeBusy;
  const acknowledged=$('#merge-confirm-uncertain');
  $('#merge-form').setAttribute('aria-busy',String(busy));
  submit.textContent=mergeBusy==='preview'?'正在核对…':mergeBusy==='confirm'?'正在合并…':
    mergeDraft?.blocked?'暂不可合并':mergeDraft?'确认合并':'核对记录';
  submit.disabled=busy||!!mergeDraft?.blocked||!!(mergeDraft?.warning&&!acknowledged?.checked);
  $('#merge-edit-selection').disabled=busy;
  $('#merge-first').disabled=busy||!!mergeDraft;
  $('#merge-second').disabled=busy||!!mergeDraft;
  const targets=$('#merge-preview .merge-targets');
  if(targets)targets.disabled=busy;
  if(acknowledged)acknowledged.disabled=busy;
  $('#merge-dialog').querySelectorAll('[data-close]').forEach(button=>{
    button.disabled=mergeBusy==='confirm';
  });
}
function showMergeDialog(matchId=null){
  if(state.matches.length<2)return;
  resetMergePreview();
  const first=$('#merge-first');
  first.innerHTML='<option value="">请选择</option>'+state.matches
    .map(match=>`<option value="${esc(match.id)}">${esc(mergeRow(match).label)}</option>`).join('');
  first.value=matchId||'';
  $('#merge-second').value='';
  renderMergeCandidates();
  $('#merge-dialog').showModal();
}
function renderMergePreview(preview,ids){
  const rows=ids.map(id=>state.matches.find(match=>match.id===id));
  if(rows.some(match=>!match))throw new Error('记录已变化，请重新选择');
  const conflicts=Array.isArray(preview.conflicts)?preview.conflicts:[];
  const conflictLabels=conflicts.map(conflict=>typeof conflict==='string'?conflict:conflict.message||conflict.label||conflict.field||'字段冲突');
  const blocked=preview.blocked||!preview.token;
  const warning=preview.warning||'';
  $('#merge-preview').innerHTML=`
    <p class="subtle">${blocked?'发现无法合并的证据，请重新选择记录。':'确认两条记录来自同一局，再选择要保留的主记录。'}</p>
    <fieldset class="merge-targets"><legend>保留的主记录</legend>${rows.map((match,i)=>{
      const row=mergeRow(match),checked=(preview.target_id?preview.target_id===match.id:i===0)?'checked':'';
      const evidence=match.evidence?.find(item=>['summary','rank'].includes(item.kind));
      const image=evidence?`<button type="button" class="text-button" data-merge-match="${esc(match.id)}" data-merge-image="${esc(evidence.index)}">查看结算原图 ↗</button>`:
        match.snapshot_backup_id?`<button type="button" class="text-button" data-merge-backup="${esc(match.snapshot_backup_id)}">查看局内快照 ↗</button>`:'';
      return `<div><label><input type="radio" name="merge-target" value="${esc(match.id)}" ${checked} ${blocked||(preview.locked_target&&match.id!==preview.locked_target)?'disabled':''}><span><strong>${esc(row.label)}</strong><small>${esc(row.pages)} · ${esc(statusLabel(match))}</small></span></label>${image}</div>`;
    }).join('')}</fieldset>
    ${conflictLabels.length?`<ul class="merge-conflicts">${conflictLabels.map(label=>`<li>${esc(label)}</li>`).join('')}</ul>`:''}
    ${warning?`<p class="merge-warning">${esc(warning)}</p><label class="radio-label"><input type="checkbox" id="merge-confirm-uncertain"> 我已核对，确认两条记录来自同一局</label>`:''}
  `;
  mergeDraft={token:preview.token,blocked,warning};
  $('#merge-selection').hidden=true;
  $('#merge-preview').hidden=false;
  $('#merge-edit-selection').hidden=false;
  if(warning)$('#merge-confirm-uncertain').onchange=updateMergeControls;
  updateMergeControls();
}

function clearMatchFilters(){
  $('#match-search').value='';
  $('#mode-filter').value='all';
  $('#status-filter').value='all';
  clearMatchTime();
  heroMatchFilter=null;
  markResultFilter('all');
}
function markResultFilter(value){
  filter=value;
  $$('[data-filter]').forEach(button=>{
    const active=button.dataset.filter===filter;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
}
function markTimePreset(hours){
  $$('[data-match-hours]').forEach(button=>{
    const active=button.dataset.matchHours===hours;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
}
function clearMatchTime(){
  setMatchBoundary('start','');
  setMatchBoundary('end','');
  markTimePreset('all');
}
function setMatchBoundary(id,value){
  const [date,hour,minute]=value.split(/[T :]/);
  $(`#match-${id}`).value=date;
  $(`#match-${id}-hour`).value=hour||'';
  $(`#match-${id}-minute`).value=minute||'';
  syncMatchBoundary(id);
}
function syncMatchBoundary(id){
  const date=$(`#match-${id}`),hour=$(`#match-${id}-hour`),minute=$(`#match-${id}-minute`);
  hour.disabled=!date.value;
  if(hour.disabled)hour.value='';
  minute.disabled=!hour.value;
  if(minute.disabled)minute.value='';
}
async function showChangelog(version=null){
  if(!showChangelog.notes){const response=await fetch('changelog.json');if(!response.ok)throw new Error('版本历史暂时未打开');showChangelog.notes=await response.json();}
  if(version&&(document.hidden||$('dialog[open]')))return false;
  const releases=version?showChangelog.notes.releases.filter(release=>release.version===version):showChangelog.notes.releases;
  if(!releases.length)return false;
  $('#changelog-content').innerHTML=releases.map(release=>`<section class="release-notes"><header><h3>${esc(release.version)}</h3><time>${esc(release.date)}</time></header><ul>${release.changes.map(change=>`<li>${esc(change)}</li>`).join('')}</ul></section>`).join('');
  const mascot=$('#tray-mascot');
  mascot.hidden=document.documentElement.dataset.artwork==='public'||!mascot.querySelector('img').naturalWidth;
  if(!$('#changelog-dialog').open)$('#changelog-dialog').showModal();
  return true;
}
function maybeShowStartupDialogs(){
  if(document.hidden||$('dialog[open]'))return;
  if(!state.config.self_name){
    if(!firstRunShown){firstRunShown=true;showUsageGuide(true);}
    return;
  }
  const version=state.whats_new;
  if(!version||version===whatsNewShown||whatsNewOpening)return;
  whatsNewOpening=true;
  action(async()=>{
    try{
      const shown=await showChangelog(version);
      if(shown){
        whatsNewShown=version;
        await call('acknowledge_whats_new',version);
      }
    }catch(error){whatsNewShown=version;throw error;}
    finally{whatsNewOpening=false;}
  });
}
function renderRecovery(){
  const jobs=state.capture_inbox||[],labels={pending:'待识别',processing:'识别中',deferred:'结算后识别',review:'待补入',failed:'待重试'};$('#recovery-count').textContent=jobs.length;
  const readout=j=>j.payload?.kind==='rank'?`${signed(j.payload.delta)} RP`:j.payload?.kind==='summary'?'三人战绩':'截图';
  $('#recovery-list').innerHTML=jobs.length?`<div class="table-panel"><table><tbody>${jobs.map(j=>`<tr><td>${esc(clock(j.created*1000))}</td><td>${readout(j)}</td><td>${labels[j.status]||j.status}</td><td class="recovery-actions"><button class="text-button" data-capture-evidence="${esc(j.id)}">查看</button>${['review','failed'].includes(j.status)?`<button class="text-button" data-recover-capture="${esc(j.id)}">补入</button>`:''}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty-state">采集线索已处理。</div>';
  const backups=state.hud_backups||[];$('#backup-count').textContent=backups.length;$('#backup-list').innerHTML=backups.length?`<div class="table-panel"><table><thead><tr><th>时间</th><th>击杀 / 助攻 / 参与</th><th>伤害</th><th>局内 RP</th><th>英雄</th><th></th></tr></thead><tbody>${backups.map(b=>`<tr><td>${esc(clock(b.time))}</td><td>${fmt(b.kills)} / ${fmt(b.assists)} / ${fmt(b.participation)}</td><td>${fmt(b.damage)}</td><td class="${tone(b.live_rp)}">${signed(b.live_rp)}</td><td class="table-name">${(b.players||[]).map(p=>esc(displayName(p.name))+' '+esc(p.hero||'—')).join(' · ')}</td><td>${b.has_image===false?'':`<button class="text-button" data-backup="${esc(b.id)}">查看</button>`}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty-state">进入对局后开始积累。</div>';
}
function profileValue(stats,field){const value=stats?.[field];if(value==null)return '—';if(stats.display_values?.[field])return esc(stats.display_values[field]);if(stats.approximate_fields?.includes(field))return value>=1000000?fmt(value/1000000,2)+'M':fmt(value/1000,2)+'K';return fmt(value,2);}
function renderProfile(){
  const p=state.profile, signature=JSON.stringify(p||null);
  if(renderProfile.signature===signature)return;
  renderProfile.signature=signature;
  $('#profile-snapshot-time').textContent=p?`${p.source==='upload'?'导入':'采集'} ${clock(p.captured_at)}${Object.keys(p.cached_fields||{}).length?' · 缺项沿用已读快照':''}`:'';
  if(!p){$('#profile-snapshot-content').innerHTML='<div class="empty-state">打开游戏内个人数据页，自动留下生涯快照。</div>';return;}
  const s=p.season||{},life=p.lifetime||{},primary=[['damage','赛季伤害'],['kills','击杀'],['kd','K/D']],secondary=[['average_damage','场均伤害'],['matches','对局'],['wins','胜利'],['top5','前 5 名']];
  const more=[['assists','助攻'],['knocks','击倒'],['highest_damage','最高伤害'],['highest_kills','最高击杀'],['win_streak','最长连胜'],['revives','救起'],['respawns','重生']];
  $('#profile-snapshot-content').innerHTML=`<div class="profile-layout"><section class="season-profile"><div class="profile-section-label"><span>${esc(p.selected_season||'所选赛季')} · ${esc(p.mode_label||'大逃杀')}</span><span>${esc(displayName(p.name))}</span></div><div class="profile-primary-metrics">${primary.map(([key,label])=>`<div><small>${label}</small><strong class="${key==='kd'?'positive':''}">${profileValue(s,key)}</strong></div>`).join('')}</div><div class="profile-secondary-metrics">${secondary.map(([key,label])=>`<div><strong>${profileValue(s,key)}</strong>${label}</div>`).join('')}</div></section><section class="career-profile"><div class="profile-section-label">生涯累积</div><div class="career-values">${[['matches','对局'],['wins','胜利'],['kills','击杀'],['damage','总伤害']].map(([key,label])=>`<div><small>${label}</small><strong>${profileValue(life,key)}</strong></div>`).join('')}</div><div class="career-level">等级 ${fmt(p.account_level)} · 生涯 ${fmt(p.career_level)} / ${fmt(p.career_level_max)}<br>阶级 ${fmt(p.prestige)} / ${fmt(p.prestige_max)} · 经验 ${fmt(p.xp)} / ${fmt(p.xp_next)}</div></section></div><details class="profile-more" open><summary>更多数据</summary><div class="profile-more-grid">${more.map(([key,name])=>`<section class="profile-stat-card"><h3>${name}</h3><div><span>赛季 <strong>${profileValue(s,key)}</strong></span><span>生涯 <strong>${profileValue(life,key)}</strong></span></div></section>`).join('')}</div></details>`;
}
function evidenceKindLabel(kind){
  return {summary:'三人战绩',rank:'分数结算',hud:'局内快照',settlement:'结算页',opening:'开局',
    lobby:'大厅分数',profile:'个人数据',manual:'手动截图',xp:'经验值结算'}[kind]||'采集截图';
}
function matchOrdinal(match){
  const ranked=(match.mode||'ranked')==='ranked';
  const rows=state.matches.filter(item=>item.season===match.season&&item.split===match.split&&
    (ranked?(item.mode||'ranked')==='ranked':item.mode===match.mode));
  rows.sort((a,b)=>a.created-b.created||String(a.id).localeCompare(String(b.id)));
  const index=rows.findIndex(row=>row.id===match.id);
  return index<0?'':`${ranked?'Rank':'Match'}#${index+1}`;
}
function detailEvidence(evidence){
  const source=(evidence||[]).map((item,index)=>({...item,index:item.index??index}));
  const button=(label,item)=>`<button type="button" class="button" ${item?`data-evidence="${esc(item.index)}"`:'disabled'}>${esc(label)}</button>`;
  const summary=source.find(item=>item.kind==='summary'),rank=source.find(item=>item.kind==='rank');
  const extras=source.filter(item=>item!==summary&&item!==rank);
  return button('三人战绩',summary)+button('分数结算',rank)+(extras.length?
    `<details class="supplement-actions detail-more"><summary class="button">其他截图</summary><div class="panel supplement-menu">${extras.map(item=>button(evidenceKindLabel(item.kind),item)).join('')}</div></details>`:'');
}
function showDetails(id,editing=false){
  currentMatch=state.matches.find(match=>match.id===id);
  if(!currentMatch)return;
  const m=currentMatch,players=m.summary?.players||[],snapshot=statusCode(m)==='snapshot';
  const start=captureTime(m.round_started_at,detailClock);
  const end=captureTime(m.round_end_at??m.settled_at,detailClock);
  const fields=[['hero','英雄'],['kills','击杀'],['assists','助攻'],['knocks','击倒'],...(snapshot?[['participation','参与']]:[]),['damage','伤害'],['revives','救起'],['respawns','重生']];
  const cell=(p,key,i)=>editing?`<input class="${['name','hero'].includes(key)?'name-input':''}" data-player="${i}" data-field="${key}" type="${['name','hero'].includes(key)?'text':'number'}" ${key==='hero'?'list="hero-names"':''} min="0" value="${esc(key==='name'?displayName(p[key]):p[key])}" aria-label="${esc(displayName(p.name))} ${key}">`:esc(key==='name'?displayName(p[key]):key==='hero'?heroName(p[key]):p[key]??'—');
  const editFields=editing?`<div class="edit-top form-row">
    <label>对局时间 · UTC+8<input id="edit-time" type="datetime-local" value="${pendingTime(m)?'':localTimeInput(m.time)}"></label>
    <label>本局 RP<input id="edit-delta" type="number" min="-3000" max="3000" value="${usesRP(m)?esc(m.rank?.delta):''}" ${usesRP(m)?'':'disabled'}></label>
    <label>排名<input id="edit-place" type="number" min="1" max="30" value="${esc(m.summary?.placement)}"></label></div>`:'';
  const table=players.length?`<div class="table-panel"><table class="detail-table"><thead><tr><th>玩家</th>${fields.map(([,label])=>`<th>${label}</th>`).join('')}</tr></thead>
    <tbody>${players.map((p,i)=>`<tr><td>${cell(p,'name',i)}${!editing&&norm(p.name)===norm(state.config.self_name)?' <span class="state-tag">我</span>':''}</td>${fields.map(([key])=>`<td>${cell(p,key,i)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:'<div class="empty-state">三人战绩待补。</div>';
  const actions=editing?'<div class="detail-action-right"><button class="button" id="cancel-edit">取消</button><button class="button primary" id="save-edit">保存</button></div>':
    `<div class="detail-action-left">${detailEvidence(m.evidence)}<button type="button" class="button" data-retry-match="${esc(m.id)}" ${retryBusy?'disabled':''}>重新识别</button></div>
     <div class="detail-action-right"><button class="button" data-upload-match="${esc(m.id)}">补充截图</button><button class="button" id="edit-match">修改数据</button><button class="button danger" id="delete-match">删除对局</button></div>`;
  $('#detail-body').innerHTML=`
    <div class="detail-meta">
      <div class="detail-result"><span class="place ${placeClass(m.summary?.placement)}">${m.summary?.placement?'#'+m.summary.placement:'—'}</span>
        ${usesRP(m)?`<strong class="delta ${tone(effectiveRp(m))}">${matchRp(m,true)}</strong>`:''}</div>
      <span class="pill">${esc(m.season)} / ${esc(m.split)} · ${modeName(m.mode)} · ${esc(matchOrdinal(m))}</span>${matchStatus(m)}
    </div>
    <div class="detail-timeline">
      <span>开局时间 <strong>${esc(start||'—')}</strong></span>
      ${editing?'':'<button type="button" class="text-button" id="match-diagnostics" aria-haspopup="dialog">采集经过</button>'}
      <span>结算时间 <strong>${esc(end||'—')}</strong></span>
      ${m.map_name?`<span>地图 <strong>${esc(m.map_name)}</strong></span>`:''}
    </div>
    ${rpDetail(m.rank)}${editFields}${table}
    ${snapshot?`<div class="detail-note">最后快照 ${esc(clock(m.snapshot_at))}</div>`:''}
    <div class="detail-actions">${actions}</div>`;
  if(!$('#detail-dialog').open)$('#detail-dialog').showModal();
}
function confirmImportPreview(result){
  const dialog=$('#import-preview-dialog');
  const kinds={summary:'总结',rank:'结算分数',hud:'局内快照',profile:'个人数据',lobby:'大厅分数'};
  $('#import-preview-message').textContent=result.message||'确认这些截图的分组后补入。';
  $('#import-preview-list').innerHTML=(result.groups||[]).map((group,index)=>
    `<li><strong>第 ${index+1} 组 · ${group.target_id?'补入已有对局':'单独处理'}</strong><ul>${group.pages.map(page=>`<li>${esc(page.name)} <small>${esc(kinds[page.kind]||'待识别')}</small></li>`).join('')}</ul></li>`).join('');
  dialog.returnValue='';
  return new Promise(resolve=>{
    dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true});
    dialog.showModal();
  });
}
async function importImages(matchId=null){
  let result=await call('import_image',matchId);
  if(result.needs_confirmation){
    if(!await confirmImportPreview(result))return;
    result=await call('confirm_image_import',result.token);
  }
  if(!result.ok){if(result.message)toast(result.message,true);return;}
  await refresh(true);
  if(matchId&&$('#detail-dialog').open)showDetails(matchId);
  const results=result.results||[],saved=results.filter(r=>r.saved).length;
  const partial=results.filter(r=>!r.saved&&!r.profile&&!r.backup&&!r.lobby);
  const message=saved?`${saved} 张截图已补入${partial.length?` · ${partial.length} 张待核对`:''}`:results.some(r=>r.profile)?'个人数据已保存':results.some(r=>r.backup)?'局内快照已保存':results.some(r=>r.lobby)?'大厅分数已更新':result.message||partial.find(r=>r.message)?.message||'未识别到战绩';
  toast(message,partial.length===results.length);
}
function refresh(force=false){
  if(refreshInFlight){
    if(!force)return refreshInFlight;
    if(!queuedForcedRefresh){
      queuedForcedRefresh=refreshInFlight.then(()=>{
        queuedForcedRefresh=null;
        return refresh(true);
      });
    }
    return queuedForcedRefresh;
  }
  refreshInFlight=fetchState(force).finally(()=>{refreshInFlight=null;});
  return refreshInFlight;
}
async function fetchState(force){
  if(!window.pywebview?.api||pendingMutations)return;
  if(document.hidden&&!force&&Date.now()-(refresh.lastHidden||0)<10000)return;if(document.hidden)refresh.lastHidden=Date.now();
  const epoch=stateEpoch;
  try{
    const packet=typeof window.pywebview.api.poll_state==='function'
      ?await call('poll_state',force?null:pollRevision,view==='matches'):{state:await call('state')};
    if(epoch!==stateEpoch||pendingMutations)return;
    pollRevision=packet.revision??null;
    if(packet.live){
      const {runtime,running,recovery}=packet.live;
      if(running!==state.running||JSON.stringify(runtime)!==JSON.stringify(state.runtime)){
        state={...state,runtime,running};
        if(!document.hidden)renderCaptureStatus();
      }
      if(recovery&&JSON.stringify([recovery.capture_inbox,recovery.hud_backups])!==JSON.stringify([state.capture_inbox,state.hud_backups])){
        state={...state,...recovery};
        if(!document.hidden&&view==='matches')renderRecovery();
      }
      maybeShowStartupDialogs();
      return;
    }
    const serialized=JSON.stringify(packet.state);
    if(force||serialized!==lastSerialized){
      state=packet.state;
      lastSerialized=serialized;
      if(!document.hidden||force)render();
    }
    maybeShowStartupDialogs();
  }catch(e){console.error(e);}
}
const settingsDrafts=new Map();
let settingsWrites=Promise.resolve(),settingsFriends=[],settingsClosing=false;
function writeSettings(buildPatch){
  const result=settingsWrites.then(async()=>{
    const patch=buildPatch();
    if(patch&&Object.keys(patch).length)applyState(await call('settings',patch));
  });
  settingsWrites=result.catch(()=>{});
  return result;
}
function settingsFeedback(saved=false){
  const status=$('#settings-save-status'),drafts=[...settingsDrafts.values()];
  const failed=drafts.find(draft=>draft.error),invalid=drafts.some(draft=>!draft.valid);
  status.classList.toggle('invalid',!!failed||invalid);
  status.textContent=failed?`设置保存失败 · ${failed.error}`:invalid?'有设置需要检查':drafts.length?'正在保存…':saved?'已自动保存':'设置自动保存';
}
function saveSetting(draft){
  if(!draft.valid||draft.queued)return;
  const {control,value}=draft,key=control.name;
  clearTimeout(draft.timer);
  draft.queued=true;
  draft.error=null;
  let nextFriends;
  writeSettings(()=>{
    if(settingsDrafts.get(key)!==draft||control.disabled)return;
    const slot=friendFields.indexOf(key);
    if(slot>=0){
      nextFriends=[...settingsFriends];
      nextFriends[slot]=value;
      const friends=nextFriends.filter(Boolean);
      return JSON.stringify(friends)!==JSON.stringify(state.config.friends)?{friends}:null;
    }
    return state.config[key]!==value?{[key]:value}:null;
  }).then(()=>{
    if(nextFriends)settingsFriends=nextFriends;
    if(settingsDrafts.get(key)===draft)settingsDrafts.delete(key);
    settingsFeedback(true);
  },error=>{
    draft.queued=false;
    draft.error=error.message||String(error);
    settingsFeedback();
  });
}
function changeSetting(control,immediate){
  if(!control.name||control.disabled||settingsClosing)return;
  const previous=settingsDrafts.get(control.name);
  if(previous)clearTimeout(previous.timer);
  const value=control.type==='checkbox'?control.checked:control.type==='number'?control.valueAsNumber:control.value.trim();
  const valid=control.checkValidity()&&(!control.required||!!String(value).trim())&&
    (control.name!=='self_name'||!!norm(value));
  control.setAttribute('aria-invalid',String(!valid));
  const draft={control,value,valid};
  settingsDrafts.set(control.name,draft);
  if(valid){
    if(immediate)saveSetting(draft);
    else draft.timer=setTimeout(()=>saveSetting(draft),700);
  }
  settingsFeedback();
}
async function closeSettings(){
  const dialog=$('#settings-dialog'),form=$('#settings-form');
  if(settingsClosing)return false;
  if(!dialog.open)return true;
  settingsClosing=true;
  form.inert=true;
  try{
    for(const draft of settingsDrafts.values())saveSetting(draft);
    await settingsWrites;
    if([...settingsDrafts.values()].some(draft=>draft.error))return false;
    if(settingsDrafts.size)toast('有无效的设置未保存',true);
    settingsDrafts.clear();
    dialog.close();
    return true;
  }finally{
    settingsClosing=false;
    form.inert=false;
  }
}
function showSettings(){
  const dialog=$('#settings-dialog');
  if(dialog.open)return;
  const form=$('#settings-form'),config={...defaults,...state.config};
  settingsFriends=[...config.friends];
  for(const control of form.elements)control.removeAttribute('aria-invalid');
  for(const key of ['self_name','season','split','theme','settlement_tone',...retentionFields,...Object.keys(highlightDefaults)]){
    form.elements[key].value=config[key]??'';
  }
  friendFields.forEach((key,i)=>{form.elements[key].value=config.friends[i]??'';});
  for(const key of checks)form.elements[key].checked=!!config[key];
  form.elements.start_on_login.disabled=state.startup?.supported===false;
  for(const key of ['season','split'])form.elements[key].disabled=!!config.auto_season;
  settingsFeedback();
  dialog.showModal();
}
const chartTip=event=>{const point=event.target.closest('.chart-point'),tip=$('#chart .chart-tooltip');if(point&&tip)tip.title=tip.textContent=point.dataset.tooltip;};
$('#hero-player').onchange=event=>{selectedPlayer=event.target.value;renderPlayerHeroes();};
function renderProfileSetup(scan=state.runtime?.profile_scan){
  const dialog=$('#profile-setup-dialog');
  if(!dialog.open)return;
  const input=$('#profile-setup-form').elements.self_name,button=$('#profile-scan-btn'),status=$('#profile-scan-status');
  button.disabled=['scanning','waiting_game','finding_lobby','reading','confirming','wrong_mode'].includes(scan?.status);
  button.textContent=button.disabled?'正在识别…':scan?'重新识别':'开始识别';
  status.dataset.state=scan?.status||'idle';
  if(['ready','candidate'].includes(scan?.status)&&scan.name){
    if(!input.dataset.edited)input.value=scan.name;
    const points=scan.total_rp!=null?`${fmt(scan.total_rp,0)} RP`
      :scan.progress!=null&&scan.capacity!=null?`${fmt(scan.progress,0)} / ${fmt(scan.capacity,0)} RP`:'';
    const rank=[scan.rank,points].filter(Boolean).join(' · ');
    status.textContent=norm(input.value)===norm(scan.name)
      ?[scan.status==='candidate'?'读到候选昵称，请核对后确认':'已识别，请核对昵称',rank||'分数稍后在排位大厅更新'].join(' · ')
      :'已改为手动填写。确认后会在排位大厅更新分数。';
  }else{
    const messages={
      scanning:'请切回 Apex 排位大厅，识别后回这里确认。',
      waiting_game:'等待 Apex 位于前台。',
      finding_lobby:'已看到 Apex，正在核对大厅画面。',
      reading:'正在读取左上角昵称和左下角分数。',
      confirming:'已读到一帧昵称，正在用下一帧确认。',
      wrong_mode:'请把左下角模式切到「排位赛」，留在游戏里继续识别。',
      expired:'这次未识别到昵称，请手动填写游戏 ID，或再试一次。'
    };
    const message=messages[scan?.status]||'';
    const reason=scan?.reason&&scan.reason!==message?scan.reason:'';
    const seconds=scan?.remaining_seconds;
    status.textContent=[message,reason,button.disabled&&Number.isFinite(seconds)&&seconds>0?`剩余 ${seconds} 秒`:''].filter(Boolean).join(' · ');
  }
}
function showProfileSetup(){
  const form=$('#profile-setup-form');
  form.reset();
  delete form.elements.self_name.dataset.edited;
  firstRunShown=true;
  if(!$('#profile-setup-dialog').open)$('#profile-setup-dialog').showModal();
  renderProfileSetup();
}
$('#profile-setup').onclick=showProfileSetup;
$('#profile-scan-btn').onclick=async()=>{
  const button=$('#profile-scan-btn'),input=$('#profile-setup-form').elements.self_name;
  const dialog=$('#profile-setup-dialog'),status=$('#profile-scan-status');
  button.disabled=true;
  try{
    const scan=await call('begin_profile_scan');
    if(!dialog.open)return;
    if(!input.dataset.edited)input.value='';
    state.runtime={...state.runtime,profile_scan:scan};
    renderProfileSetup(scan);
  }catch(error){
    button.disabled=false;
    status.dataset.state='expired';
    status.textContent=error.message||'无法开始识别，请手动填写游戏 ID。';
  }
};
$('#profile-setup-form').elements.self_name.oninput=event=>{
  event.target.dataset.edited='true';
  renderProfileSetup();
};
$('#profile-setup-form').onsubmit=event=>{
  event.preventDefault();
  action(async()=>{
    const button=event.submitter||event.target.querySelector('[type=submit]');
    button.disabled=true;
    try{
      applyState(await call('confirm_initial_profile',event.target.elements.self_name.value));
      $('#profile-setup-dialog').close();
      toast('本机档案已建立');
    }catch(error){
      const status=$('#profile-scan-status');
      status.dataset.state='expired';
      status.textContent=error.message||'档案未保存，请核对游戏昵称。';
    }finally{button.disabled=false;}
  });
};
$('#profile-setup-dialog').addEventListener('close',()=>{
  delete state.runtime.profile_scan;
  if(window.pywebview?.api)action(()=>call('cancel_profile_scan'));
});
$('#profile-setup-settings').onclick=()=>{
  $('#profile-setup-dialog').close();
  showSettings();
};
function showProfileChange(){
  const change=state.runtime?.profile_change,dialog=$('#profile-change-dialog');
  if(!change||dialog.open)return;
  profileChangeShown=JSON.stringify([change.current,change.detected]);
  $('#profile-change-error').hidden=true;
  dialog.showModal();
}
function renderProfileChange(change=state.runtime?.profile_change){
  const dialog=$('#profile-change-dialog');
  $('#profile-change-notice').hidden=!change;
  if(!change){
    profileChangeShown=null;
    if(dialog.open)dialog.close();
    return;
  }
  $('#profile-change-current').textContent=change.current;
  $('#profile-change-detected').textContent=change.detected;
  $('#profile-change-message').textContent=`大厅昵称为「${change.detected}」，请确认后继续记录。`;
  const key=JSON.stringify([change.current,change.detected]);
  if(dialog.open)profileChangeShown=key;
  if(key!==profileChangeShown&&!$('dialog[open]'))showProfileChange();
}
$('#profile-change-open').onclick=showProfileChange;
document.addEventListener('close',()=>renderProfileChange(),true);
$$('[data-profile-choice]').forEach(button=>button.onclick=async()=>{
  const controls=$$('[data-profile-choice]'),errorMessage=$('#profile-change-error');
  controls.forEach(control=>control.disabled=true);
  errorMessage.hidden=true;
  try{
    const choice=button.dataset.profileChoice;
    applyState(await call('resolve_profile_change',choice));
    toast(choice==='merge'?'昵称已合并，原档案已备份':'已切换到独立档案');
  }catch(error){
    errorMessage.textContent=error.message||'档案更新失败，请重试。';
    errorMessage.hidden=false;
  }finally{controls.forEach(control=>control.disabled=false);}
});
$('#chart').addEventListener('pointerover',chartTip);$('#chart').addEventListener('focusin',chartTip);$('#chart').addEventListener('click',chartTip);
window.addEventListener('resize',()=>{if(view==='overview')renderChart();resizeChartText();fitHeroArt();});
document.addEventListener('click',event=>{
  const win=event.target.closest('[data-window]');if(win){action(async()=>{
    if(win.dataset.window==='close'&&!await closeSettings())return;
    await call('window_control',win.dataset.window);
  });return;}
  const folder=event.target.closest('[data-open-folder]');if(folder){action(()=>call('open_folder',folder.dataset.openFolder));return;}
  const close=event.target.closest('[data-close]');if(close){
    if(close.dataset.close==='settings-dialog')action(closeSettings);
    else $(`#${close.dataset.close}`).close();
    return;
  }
  const playerLink=event.target.closest('[data-player-heroes]');
  if(playerLink){selectedPlayer=playerLink.dataset.playerHeroes;navigate('teammates');$('#hero-player').focus();return;}
  const heroLink=event.target.closest('[data-hero-matches]');
  if(heroLink){
    const player=(state.player_hero_stats||[]).find(p=>p.key===selectedPlayer);
    if(!player)return;
    clearMatchFilters();
    heroMatchFilter={key:player.key,label:player.name,name:heroLink.dataset.heroMatches};
    navigate('matches');renderMatches();$('#hero-match-filter-clear').focus();return;
  }
  if(event.target.closest('[data-clear-match-filters]')){clearMatchFilters();renderMatches();$('#match-search').focus();return;}
  if(event.target.closest('#hero-match-filter-clear')){heroMatchFilter=null;renderMatches();$('#match-search').focus();return;}
  const nav=event.target.closest('[data-nav]');if(nav){navigate(nav.dataset.nav);return;}
  const retry=event.target.closest('[data-retry-match]');if(retry){action(()=>retryMatch(retry));return;}
  const upload=event.target.closest('[data-upload-match]');if(upload){action(()=>importImages(upload.dataset.uploadMatch));return;}
  const match=event.target.closest('[data-match]');if(match){showDetails(match.dataset.match);return;}
  if(event.target.closest('#match-diagnostics')){action(()=>showCaptureDiagnostics(currentMatch.id));return;}
  const evidence=event.target.closest('[data-evidence]');if(evidence){action(async()=>{$('#evidence-image').src=await call('evidence',currentMatch.id,evidence.dataset.evidence);$('#image-dialog').showModal();});return;}
  const capture=event.target.closest('[data-capture-evidence]');if(capture){action(async()=>{$('#evidence-image').src=await call('capture_evidence',capture.dataset.captureEvidence);$('#image-dialog').showModal();});return;}
  const backup=event.target.closest('[data-backup]');if(backup){action(async()=>{$('#evidence-image').src=await call('backup_evidence',backup.dataset.backup);$('#image-dialog').showModal();});return;}
  const recover=event.target.closest('[data-recover-capture]');if(recover){action(async()=>{const result=await call('recover_capture',recover.dataset.recoverCapture);applyState(result.state);toast(result.ok?'已补入对局':'这张图还需核对',!result.ok);});return;}
  if(event.target.closest('#edit-match'))showDetails(currentMatch.id,true);
  if(event.target.closest('#cancel-edit'))showDetails(currentMatch.id);
  if(event.target.closest('#save-edit'))action(async()=>{
    const match=currentMatch,patch={},time=$('#edit-time'),delta=$('#edit-delta'),placement=$('#edit-place');
    if(!time.checkValidity()||(time.value&&!MatchLogFilters.timeRange(time.value).valid))throw new Error('请输入有效的对局时间');
    if(time.value&&time.value!==time.defaultValue)patch.time=time.value+':00+08:00';
    if(!delta.disabled&&delta.value!==delta.defaultValue)patch.delta=delta.value;
    if(placement.value!==placement.defaultValue)patch.placement=placement.value;
    if(match.summary){
      const players=match.summary.players.map((_,i)=>Object.fromEntries($$(`[data-player="${i}"]`)
        .filter(input=>input.value!==input.defaultValue).map(input=>[input.dataset.field,input.value])));
      if(players.some(player=>Object.keys(player).length))patch.players=players;
    }
    if(Object.keys(patch).length){applyState(await call('edit',match.id,patch));toast('已更新');}
    showDetails(match.id);
  });
  if(event.target.closest('#delete-match')&&!$('#delete-prompt'))$('#detail-body').insertAdjacentHTML('beforeend','<div class="deletion-prompt" id="delete-prompt">删除这局？<div><button class="button primary" id="confirm-delete">删除</button><button class="button" id="cancel-delete">取消</button></div></div>');
  if(event.target.closest('#cancel-delete'))$('#delete-prompt').remove();
  if(event.target.closest('#confirm-delete'))action(async()=>{const id=currentMatch.id;$('#detail-dialog').close();applyState(await call('delete',id));toast('对局已删除',false,id);});
});
document.addEventListener('keydown',event=>{const row=event.target.closest('[data-match]');if(row&&(event.key==='Enter'||event.key===' ')){event.preventDefault();showDetails(row.dataset.match);}});
$('#record-control').onclick=event=>action(async()=>{
  if(!state.config.self_name){
    if(await closeSettings())showProfileSetup();
    return;
  }
  const button=event.currentTarget;
  button.disabled=true;
  try{applyState(await call('toggle'));}
  finally{button.disabled=false;}
});
function showUsageGuide(onboarding=false){
  const button=$('#usage-return');
  button.textContent=onboarding?'设置档案':'返回';
  button.classList.toggle('primary',onboarding);
  button.onclick=onboarding?event=>{
    event.stopPropagation();
    $('#usage-dialog').close();
    showProfileSetup();
  }:null;
  $('#usage-dialog').showModal();
}
$('#usage-btn').onclick=()=>showUsageGuide();
$('#usage-feedback').onclick=()=>action(async()=>{
  $('#usage-dialog').close();
  await showCaptureDiagnostics();
});
$('#website-btn').onclick=()=>action(()=>call('open_website'));
async function retryMatch(button){
  if(retryBusy)return;
  retryBusy=true;
  const id=button.dataset.retryMatch;
  button.disabled=true;
  button.textContent='正在检查截图…';
  try{
    const result=await call('retry_saved_captures',id);
    if(result.state)applyState(result.state);
    toast(result.queued?`已安排重试 ${result.queued} 张截图`:'这局没有待补全的可重试截图');
  }finally{
    retryBusy=false;
    if(button.isConnected){
      button.textContent='重新识别';
      button.disabled=false;
    }
  }
}
let diagnosticsMatchId=null,diagnosticsLoad=0;
let cloudLogDownloadBusy=false;
async function showCloudLogDownload(matchId){
  const button=$('#cloud-logs-download');
  button.hidden=true;
  if(matchId||!window.pywebview?.api?.cloud_log_admin_status)return;
  const access=await call('cloud_log_admin_status');
  if(diagnosticsMatchId!==matchId||!$('#capture-diagnostics-dialog').open)return;
  button.hidden=!access.available;
  button.dataset.requiresPin=String(access.requires_pin);
}
async function downloadCloudLogs(pin=null){
  if(cloudLogDownloadBusy)return;
  const button=$('#cloud-logs-download');
  cloudLogDownloadBusy=button.disabled=true;
  button.textContent='正在下载…';
  try{
    const result=await call('export_cloud_diagnostics',pin);
    if(result.ok)toast(`已下载 ${fmt(result.reports,0)} 份报告 · ${fmt(result.events,0)} 条事件`);
  }finally{
    cloudLogDownloadBusy=button.disabled=false;
    button.textContent='下载云端日志';
  }
}
$('#cloud-logs-download').onclick=()=>action(async()=>{
  if($('#cloud-logs-download').dataset.requiresPin!=='true'){
    await downloadCloudLogs();
    return;
  }
  const dialog=$('#cloud-logs-auth');
  $('#cloud-logs-auth-form').reset();
  dialog.showModal();
});
$('#cloud-logs-auth-form').onsubmit=event=>{
  event.preventDefault();
  const form=event.currentTarget;
  const pin=form.elements.pin.value;
  form.reset();
  $('#cloud-logs-auth').close();
  return action(()=>downloadCloudLogs(pin));
};
async function refreshCaptureDiagnostics(){
  const ticket=++diagnosticsLoad,matchId=diagnosticsMatchId;
  const value=await call('capture_diagnostics',matchId);
  if(ticket!==diagnosticsLoad||!$('#capture-diagnostics-dialog').open)return;
  const text=$('#capture-diagnostics-text');
  text.value=value;
  text.scrollTop=text.scrollHeight;
}
async function showCaptureDiagnostics(matchId=null){
  diagnosticsMatchId=matchId;
  $('#capture-diagnostics-title').textContent=matchId?'采集经过':'采集日志';
  $('#capture-diagnostics-note').textContent=matchId?'查看该局的采集和入档经过。导出报告会隐去本机路径和玩家昵称。':'最近的采集事件会保存在本机，重启后仍可查看。导出还包含过去 24 小时的场次、截图尺寸和状态，以及 Windows 和显示信息。';
  $('#capture-diagnostics-text').value='正在读取…';
  $('#diagnostics-upload').hidden=!!matchId;
  $('#diagnostics-range input[value="24h"]').checked=true;
  resetDiagnosticsPreview();
  $('#capture-diagnostics-dialog').showModal();
  await Promise.all([refreshCaptureDiagnostics(),showCloudLogDownload(matchId).catch(()=>{})]);
}
let diagnosticsToken=null, diagnosticsUploadRevision=0, diagnosticsPreviewShown=false;
const diagnosticsEmpty=scope=>scope==='none'?'请填写反馈说明。':'所选范围内没有日志，可以填写反馈说明。';
function resetDiagnosticsPreview(){
  diagnosticsUploadRevision++;
  diagnosticsToken=null;
  diagnosticsPreviewShown=false;
  $('#diagnostics-range').disabled=false;
  $('#diagnostics-feedback').disabled=false;
  $('#diagnostics-preview').disabled=false;
  $('#diagnostics-submit').disabled=false;
  $('#capture-diagnostics-refresh').disabled=false;
  $('#diagnostics-upload-status').textContent='匿名日志包含采集时间、事件类型和报告内编号。';
}
async function diagnosticsAction(operation){
  const revision=++diagnosticsUploadRevision;
  const scope=$('#diagnostics-range input:checked').value;
  const feedback=$('#diagnostics-feedback').value;
  const active=()=>revision===diagnosticsUploadRevision&&$('#capture-diagnostics-dialog').open;
  $('#diagnostics-range').disabled=true;
  $('#diagnostics-feedback').disabled=true;
  $('#diagnostics-preview').disabled=true;
  $('#diagnostics-submit').disabled=true;
  $('#capture-diagnostics-refresh').disabled=true;
  try{await operation(scope,feedback,active);}
  catch(error){if(active())$('#diagnostics-upload-status').textContent=error.message||String(error);}
  finally{
    if(active()){
      $('#diagnostics-range').disabled=false;
      $('#diagnostics-feedback').disabled=false;
      $('#diagnostics-preview').disabled=false;
      $('#diagnostics-submit').disabled=false;
      $('#capture-diagnostics-refresh').disabled=false;
    }
  }
}
$('#diagnostics-range').onchange=()=>action(async()=>{
  resetDiagnosticsPreview();
  await refreshCaptureDiagnostics();
});
$('#diagnostics-feedback').oninput=()=>{
  const showing=diagnosticsPreviewShown;
  resetDiagnosticsPreview();
  if(showing)return action(refreshCaptureDiagnostics);
};
$('#diagnostics-preview').onclick=()=>diagnosticsAction(async(scope,feedback,active)=>{
  diagnosticsToken=null;
  $('#diagnostics-upload-status').textContent='正在准备预览…';
  const draft=await call('preview_diagnostics_upload',scope,feedback);
  if(!active())return;
  diagnosticsLoad++;
  diagnosticsToken=draft.token;
  diagnosticsPreviewShown=true;
  const text=$('#capture-diagnostics-text');
  text.value=[draft.feedback?`反馈说明\n${draft.feedback}`:'',draft.text].filter(Boolean).join('\n\n')||diagnosticsEmpty(scope);
  text.scrollTop=0;
  const size=draft.count===draft.shown?'':` · 预览最近 ${fmt(draft.shown,0)} 条`;
  const dates=draft.count?` · ${captureTime(draft.first_at,clock)}—${captureTime(draft.last_at,clock)}`:'';
  $('#diagnostics-upload-status').textContent=draft.count
    ?`待提交 ${fmt(draft.count,0)} 条匿名事件${draft.feedback?'和反馈说明':''}${dates}${size}`
    :draft.feedback?'待提交反馈说明。':diagnosticsEmpty(scope);
});
$('#diagnostics-submit').onclick=()=>diagnosticsAction(async(scope,feedback,active)=>{
  $('#diagnostics-upload-status').textContent='正在提交匿名反馈…';
  if(!diagnosticsToken){
    const draft=await call('preview_diagnostics_upload',scope,feedback);
    if(!active())return;
    diagnosticsToken=draft.token;
    if(!diagnosticsToken){
      $('#diagnostics-upload-status').textContent=diagnosticsEmpty(scope);
      return;
    }
  }
  let result;
  try{
    result=await call('upload_diagnostics',diagnosticsToken);
  }catch(error){
    if(active()&&error.message?.includes('上传预览已失效')){
      diagnosticsToken=null;
      throw new Error('这份反馈预览已过期，点击提交可重新准备。');
    }
    throw error;
  }
  if(!active())return;
  diagnosticsToken=null;
  $('#diagnostics-feedback').value='';
  const submitted=result.count?`${fmt(result.count,0)} 条日志${feedback.trim()?'和反馈说明':''}`:'反馈说明';
  $('#diagnostics-upload-status').textContent=`已提交${result.count?' ':''}${submitted} · 回执 ${result.receipt}`;
});
let anonymousToken=null;
function resetAnonymousPreview(){
  anonymousToken=null;
  $('#anonymous-send').hidden=true;
}
function showAnonymousStatus(value){
  $('#anonymous-enabled').checked=value.enabled;
  $('#anonymous-preview').disabled=!value.enabled;
  $('#anonymous-summary').disabled=!value.enabled;
  $('#anonymous-status').textContent=value.enabled
    ?value.last_upload?`最近上传 ${captureTime(value.last_upload,fullClock)}`:'已开启，预览后可以提交'
    :'已关闭';
}
async function updateAnonymousStatus(){showAnonymousStatus(await call('community_status'));}
async function anonymousAction(button, operation){
  const buttons=['anonymous-enabled','anonymous-preview','anonymous-summary','anonymous-send'].map(id=>$('#'+id));
  const disabled=buttons.map(control=>control.disabled);
  buttons.forEach(control=>{control.disabled=true;});
  button.setAttribute('aria-busy','true');
  try{await operation();}
  catch(error){$('#anonymous-status').textContent=error.message||String(error);}
  finally{
    buttons.forEach((control,i)=>{control.disabled=disabled[i];});
    const enabled=$('#anonymous-enabled').checked;
    $('#anonymous-preview').disabled=!enabled;
    $('#anonymous-summary').disabled=!enabled;
    button.removeAttribute('aria-busy');
  }
}
$('#anonymous-enabled').onchange=event=>{
  const control=event.currentTarget,enabled=control.checked;
  anonymousAction(control,async()=>{
    resetAnonymousPreview();
    try{showAnonymousStatus(await call('set_community_enabled',enabled));}
    catch(error){control.checked=!enabled;throw error;}
  });
};
$('#anonymous-preview').onclick=event=>anonymousAction(event.currentTarget,async()=>{
  resetAnonymousPreview();
  const result=await call('preview_community_upload');
  anonymousToken=result.count?result.token:null;
  $('#anonymous-send').hidden=!anonymousToken;
  $('#anonymous-status').textContent=result.count?`本次将发送 ${result.count} 局的匿名统计字段。`:'没有符合条件的新对局。对局结束 1 小时后可以提交。';
});
$('#anonymous-send').onclick=event=>anonymousAction(event.currentTarget,async()=>{
  const token=anonymousToken;
  resetAnonymousPreview();
  if(!token)return;
  $('#anonymous-status').textContent='正在发送匿名统计…';
  const result=await call('upload_community_matches',token);
  $('#anonymous-status').textContent=`腾讯云已确认 ${result.count} 局。`;
});
$('#anonymous-summary').onclick=event=>anonymousAction(event.currentTarget,async()=>{
  const result=await call('community_summary');
  if(!result?.ready){$('#anonymous-status').textContent='有效样本暂不足，待有更多参与者后显示社区汇总。';return;}
  const local=state.matches.filter(m=>m.mode==='ranked'&&complete(m)).map(m=>own(m)?.damage).filter(Number.isFinite);
  const damage=avg(local),deciles=result.damage_deciles||[];
  const comparison=local.length>=3&&deciles.length===9?` · 本机约处于前 ${100-10*deciles.filter(cut=>damage>=cut).length}%`:'';
  $('#anonymous-status').textContent=`本赛段排位 · ${fmt(result.players,0)} 位玩家 · ${fmt(result.matches,0)} 局 · 社区场均伤害 ${fmt(result.average_damage)}${comparison}`;
});
$('#community-btn')?.addEventListener('click',()=>action(async()=>{
  $('#settings-dialog').close();
  resetAnonymousPreview();
  $('#community-dialog').showModal();
  await updateAnonymousStatus();
}));
$('#capture-diagnostics-btn').onclick=()=>action(showCaptureDiagnostics);
$('#capture-diagnostics-refresh').onclick=()=>action(async()=>{
  resetDiagnosticsPreview();
  await refreshCaptureDiagnostics();
});
$('#capture-diagnostics-copy').onclick=()=>action(async()=>{
  const text=$('#capture-diagnostics-text');
  try{
    await navigator.clipboard.writeText(text.value);
    toast('采集日志已复制');
  }catch{
    text.focus();
    text.select();
    toast(document.execCommand('copy')?'采集日志已复制':'日志已选中，按 Ctrl+C 复制');
  }
});
$('#capture-diagnostics-export').onclick=()=>action(async()=>{
  const result=await call('export_capture_diagnostics',{
    device_pixel_ratio:window.devicePixelRatio,
    viewport_css:[window.innerWidth,window.innerHeight],
    screen_css:[window.screen.width,window.screen.height],
  },diagnosticsMatchId);
  if(result.ok)toast('日志报告已保存');
});
$('#shuffle-hero').onclick=()=>action(chooseHero);
$('#friends-recent-btn').onclick=()=>{
  renderRecentFriends();
  $('#friends-recent-dialog').showModal();
};
$('#import-btn').onclick=()=>action(()=>importImages());
$('#merge-matches-btn').onclick=()=>showMergeDialog();
$('#merge-first').onchange=renderMergeCandidates;
$('#merge-edit-selection').onclick=resetMergePreview;
$('#merge-dialog').addEventListener('cancel',event=>{
  if(mergeBusy==='confirm')event.preventDefault();
});
$('#merge-dialog').addEventListener('close',()=>{
  if(!$('#merge-dialog').open)resetMergePreview();
});
$('#merge-preview').onclick=event=>{
  const button=event.target.closest('[data-merge-image],[data-merge-backup]');
  if(!button)return;
  action(async()=>{
    const source=button.dataset.mergeBackup?await call('backup_evidence',button.dataset.mergeBackup):
      await call('evidence',button.dataset.mergeMatch,button.dataset.mergeImage);
    if(!$('#merge-dialog').open||!button.isConnected)return;
    $('#evidence-image').src=source;
    $('#image-dialog').showModal();
  });
};
$('#merge-form').onsubmit=async event=>{
  event.preventDefault();
  const button=$('#merge-submit');
  if(button.disabled||mergeBusy)return;
  const revision=mergeRevision;
  const ids=[$('#merge-first').value,$('#merge-second').value];
  mergeBusy=mergeDraft?'confirm':'preview';
  updateMergeControls();
  mergeError();
  try{
    if(mergeDraft){
      const target=$('#merge-preview input[name="merge-target"]:checked')?.value;
      if(!target)throw new Error('请选择要保留的主记录');
      const acknowledged=$('#merge-confirm-uncertain')?.checked===true;
      applyState(await call('confirm_merge_matches',mergeDraft.token,target,acknowledged));
      $('#merge-dialog').close();
      toast('已合并为一局，可在对局记录撤销');
    }else{
      if(!ids.every(Boolean)||ids[0]===ids[1])throw new Error('请选择两条不同的记录');
      const preview=await call('preview_merge_matches',ids);
      if(revision===mergeRevision&&$('#merge-dialog').open)renderMergePreview(preview,ids);
    }
  }catch(error){
    if(revision===mergeRevision)mergeError(error.message||String(error));
  }finally{
    if(revision===mergeRevision){
      mergeBusy=null;
      updateMergeControls();
    }
  }
};
$('#undo-merge-btn').onclick=()=>action(async()=>{
  applyState(await call('undo_merge_matches'));
  toast('已恢复合并前的两条记录');
});
$('#export-btn').onclick=()=>action(async()=>{
  const ids=matchSelection().matches.map(match=>match.id);
  if(!ids.length)return;
  const result=await call('export',ids);
  if(result.ok)toast(`已导出 ${result.count} 局`);
});
$('#match-search').oninput=renderMatches;$('#mode-filter').onchange=renderMatches;$('#status-filter').onchange=renderMatches;
for(const id of ['start','end']){
  const change=()=>{syncMatchBoundary(id);markTimePreset(null);renderMatches();};
  $(`#match-${id}`).onchange=change;
  for(const [part,count] of [['hour',24],['minute',60]]){
    const select=$(`#match-${id}-${part}`);
    for(let i=0;i<count;i++){
      const value=String(i).padStart(2,'0');
      select.add(new Option(value,value));
    }
    select.onchange=change;
  }
}
$$('[data-match-hours]').forEach(button=>button.onclick=()=>{
  const hours=button.dataset.matchHours;
  if(hours==='all'){clearMatchTime();$('#custom-time').open=false;renderMatches();return;}
  $('#custom-time').open=false;
  const [start,end]=MatchLogFilters.recentTimeInputs(Number(hours));
  setMatchBoundary('start',start);
  setMatchBoundary('end',end);
  markTimePreset(hours);
  renderMatches();
});
$('#match-time-clear').onclick=()=>{clearMatchTime();$('#custom-time').open=false;renderMatches();};
$('#changelog-btn').onclick=()=>action(showChangelog);
$('#release-update').onclick=()=>action(()=>call('open_releases'));
async function checkReleaseUpdate(){
  const api=window.pywebview?.api;
  if(!api?.check_release_update||checkReleaseUpdate.started)return;
  checkReleaseUpdate.started=true;
  try{
    const result=await api.check_release_update();
    if(result.status!=='available')return;
    const button=$('#release-update');
    button.textContent=`更新 ${result.version} 可用 ↗`;
    button.title='在浏览器打开下载页';
    button.hidden=false;
  }catch{ /* A version check must not interrupt recording or startup. */ }
}
if(window.pywebview?.api?.check_release_update)checkReleaseUpdate();
else window.addEventListener('pywebviewready',checkReleaseUpdate,{once:true});
$$('[data-filter]').forEach(el=>el.onclick=()=>{markResultFilter(el.dataset.filter);renderMatches();});
$('#settings-btn').onclick=showSettings;
$('#settings-form').oninput=event=>{
  if(!event.isComposing&&['text','number'].includes(event.target.type))changeSetting(event.target,false);
};
$('#settings-form').addEventListener('compositionstart',event=>clearTimeout(settingsDrafts.get(event.target.name)?.timer));
$('#settings-form').addEventListener('compositionend',event=>changeSetting(event.target,false));
$('#settings-form').onchange=event=>{
  const control=event.target;
  if(control.name==='auto_season'){
    for(const key of ['season','split']){
      const field=control.form.elements[key];
      field.disabled=control.checked;
      if(field.disabled){
        clearTimeout(settingsDrafts.get(key)?.timer);
        settingsDrafts.delete(key);
        field.value=state.config[key];
        field.removeAttribute('aria-invalid');
      }
    }
  }
  if(['checkbox','select-one'].includes(control.type))changeSetting(control,true);
};
$('#settings-form').onsubmit=event=>{event.preventDefault();action(closeSettings);};
$('#settings-dialog').addEventListener('cancel',event=>{event.preventDefault();action(closeSettings);});
$('#sound-preview').onclick=()=>action(async()=>{
  const tone=$('#settings-form').elements.settlement_tone.value;
  const result=await call('preview_settlement_sound',tone);
  if(!result?.ok)throw new Error('音效暂时不可用，请稍后再试');
});
$('#cleanup-btn').onclick=()=>{$('#cleanup-result').textContent='';$('#cleanup-dialog').showModal();};
$('#cleanup-form').onsubmit=event=>{event.preventDefault();action(async()=>{const result=await call('cleanup_images',new FormData(event.target).get('mode'));applyState(result.state);$('#cleanup-result').textContent=`${result.files} 张 · ${fmt(result.bytes/1024/1024,2)} MB`;toast('截图已清理');});};
$$('dialog').forEach(el=>el.addEventListener('click',event=>{
  if(event.target!==el)return;
  const r=el.getBoundingClientRect();
  if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom){
    if(el.id==='settings-dialog')action(closeSettings);
    else el.close();
  }
}));
document.addEventListener('visibilitychange',()=>{if(!document.hidden){render();refresh();}});window.addEventListener('pywebviewready',()=>refresh(true));
MapView.start();render();initHeroCard();refresh();setInterval(refresh,1000);

$('.window-drag').ondblclick=()=>action(()=>call('window_control','maximize'));
function backupDetails(result){
  const parts=[];
  if(result.capture_jobs)parts.push(`${result.capture_jobs} 条待处理截图`);
  if(result.portrait_samples)parts.push(`${result.portrait_samples} 个头像样本`);
  if(result.schedule)parts.push('赛季日程');
  if(result.schedule_preserved)parts.push('已保留本机日程');
  if(result.capture_jobs_skipped)parts.push(`跳过 ${result.capture_jobs_skipped} 条线索`);
  return parts.length?' · '+parts.join(' · '):'';
}
async function saveBackup(){
  const result=await call('export_backup');
  if(result.ok)toast(`已备份 ${result.matches} 局 · ${result.images} 张图`+backupDetails(result));
}
$('#backup-export').onclick=()=>action(saveBackup);
$('#backup-import').onclick=()=>action(async()=>{
  const result=await call('import_backup');
  if(result.ok){
    applyState(result.state);
    toast(`已导入 ${result.imported} 局 · 跳过重复 ${result.skipped} 局`+backupDetails(result));
  }
});
$('#reset-btn').onclick=()=>{$('#reset-content').innerHTML='<p class="reset-description">清空全部对局、分数校准、个人资料与截图。设置、备份和素材保留。</p><p class="reset-backup-note">建议先导出一份备份。</p><div class="detail-actions"><button class="button" id="reset-backup">先导出备份</button><button class="button" id="reset-next">下一步</button></div>';$('#reset-dialog').showModal();};
let resetToken=null;
$('#reset-content').onclick=event=>{
  if(event.target.closest('#reset-backup'))action(saveBackup);
  if(event.target.closest('#reset-next'))action(async()=>{
    const result=await call('prepare_reset');
    resetToken=result.token;
    $('#reset-content').innerHTML=`<p class="reset-description">确定清空全部 <strong>${result.matches}</strong> 局及相关数据？</p><div class="detail-actions"><button class="button danger-button" id="reset-confirm">确认清空</button><button class="button" data-close="reset-dialog">取消</button></div>`;
  });
  if(event.target.closest('#reset-confirm'))action(async()=>{
    event.target.disabled=true;
    try{
      const result=await call('reset_data',resetToken);
      applyState(result);
      resetToken=null;
      $('#reset-dialog').close();
      await closeSettings();
      if(result.reset_cleanup_errors)toast(`记录已清空，有 ${result.reset_cleanup_errors} 张截图未删除`,true);
      else toast('本地数据已清空');
    }finally{event.target.disabled=false;}
  });
};
setInterval(()=>{if(!document.hidden){renderOverview();renderMatches();}},60000);

$('#new-match-btn').onclick=()=>{if(!state.config.self_name){showSettings();return;}const form=$('#new-match-form');form.reset();form.elements.time.value=localTimeInput(Date.now());form.elements.mode.value=['ranked','casual','event'].includes(state.config.detected_mode)?state.config.detected_mode:'ranked';updateManualRP(form);$('#manual-players').innerHTML=[state.config.self_name,'',''].map((name,i)=>`<fieldset class="manual-player"><legend>${i===0?'本人':`队友 ${i}（选填）`}</legend><div class="manual-player-fields"><label>昵称<input data-manual="${i}" data-key="name" value="${esc(name)}" ${i===0?'required readonly':''}></label><label>英雄<input data-manual="${i}" data-key="hero" list="hero-names"></label>${[['kills','击杀'],['assists','助攻'],['knocks','击倒'],['damage','伤害']].map(([key,label])=>`<label>${label}<input data-manual="${i}" data-key="${key}" type="number" min="0" max="100000"></label>`).join('')}</div></fieldset>`).join('');$('#new-match-dialog').showModal();};
function updateManualRP(form){form.elements.delta.disabled=!usesRP({mode:form.elements.mode.value});}
$('#new-match-form [name=mode]').onchange=event=>updateManualRP(event.target.form);
$('#new-match-form').onsubmit=event=>{event.preventDefault();action(async()=>{const f=event.target;const players=[0,1,2].map(i=>Object.fromEntries($$(`[data-manual="${i}"]`).map(input=>[input.dataset.key,input.value]))).filter(p=>p.name.trim());applyState(await call('create_match',{time:f.elements.time.value+':00+08:00',mode:f.elements.mode.value,placement:f.elements.placement.value,delta:f.elements.delta.disabled?'':f.elements.delta.value,players}));$('#new-match-dialog').close();navigate('matches');toast('记录已添加');});};
