'use strict';
const MapView = (() => {
  const mapNames = new Map([
    ['storm point',['风暴点','storm-point','暴风点','暴風點']],
    ['e-district',['电力区域','e-district']],
    ['e-district night',['电流区夜间','e-district','電流區夜間']],
    ['broken moon',['残月','broken-moon','殘月']],
    ["world's edge",['世界尽头','worlds-edge','世界边缘','世界邊緣']],
    ['olympus',['奥林匹斯','olympus','奧林匹斯']],
    ['kings canyon',['诸王峡谷','kings-canyon']],
    ['fragment',['碎片','worlds-edge']],
    ['barometer',['晴雨表','storm-point']],
  ]);
  const eventNames = {TDM:'团队死斗','Team Deathmatch':'团队死斗',Control:'控制','Gun Run':'子弹时间'};
  const modes = [['ranked','排位'],['pubs','匹配'],['mixtape','轮换模式']];
  const mapInfo = name => mapNames.get(name?.toLowerCase()) || [...mapNames.values()]
    .find(([label,,...aliases])=>label===name||aliases.includes(name)) || [name||'—',null];
  let latest = null, pending = false, failed = false, nextRequest = 0, statsSignature = null;

  function remaining(seconds){
    const whole=Math.max(0,Math.ceil(seconds)),minutes=Math.floor(whole/60);
    const digits=n=>String(n).padStart(2,'0');
    return (minutes>=60?Math.floor(minutes/60)+':':'')+digits(minutes%60)+':'+digits(whole%60);
  }

  function slotsAt(mode,now){
    const slots=latest?.modes?.[mode]||{};
    const sequence=[slots.current,slots.next].filter(Boolean);
    return {
      current:sequence.find(slot=>slot.start<=now&&now<slot.end),
      upcoming:sequence.find(slot=>slot.start>now),
    };
  }

  function clockNow(){
    return latest?latest.now+(performance.now()-latest.loaded)/1000:Date.now()/1000;
  }

  function rankedCaption(now){
    const {current}=slotsAt('ranked',now);
    const caption=$('#ranked-map-caption');
    const minutes=current?Math.ceil((current.end-now)/60):0;
    const hours=Math.floor(minutes/60);
    const time=hours?`${hours} 小时 ${minutes%60} 分`:`${minutes} 分钟`;
    const text=current?`排位地图 · ${mapInfo(current.map)[0]} · 剩余 ${time}`:'排位地图 · —';
    if(caption.textContent!==text){
      caption.textContent=text;
      caption.title=text;
    }
  }

  function panel(mode,label,now){
    const {current,upcoming}=slotsAt(mode,now);
    const [name,art]=mapInfo((current||upcoming)?.map);
    const sub=eventNames[current?.event]||current?.event||'';
    const currentLabel=[name,sub].filter(Boolean).join(' · ');
    const nextLabel=upcoming?[mapInfo(upcoming.map)[0],eventNames[upcoming.event]||upcoming.event].filter(Boolean).join(' · '):'—';
    const timer=current||upcoming;
    const deadline=current?.end??upcoming?.start;
    return `<article class="panel map-slot">
      ${art&&document.documentElement?.dataset.artwork!=='public'?`<img src="assets/maps/${art}.webp" alt="">`:''}
      <div class="map-slot-body"><h3>${label}</h3><strong>${current?esc(currentLabel):'当前地图待更新'}</strong>
        <div class="map-slot-time">${timer?`${current?'剩余':'距下一场'} <time data-rotation-end="${deadline}">${remaining(deadline-now)}</time>`:'—'}</div>
      </div><div class="map-slot-next">下一场 <b>${esc(nextLabel)}</b></div></article>`;
  }

  function rotation(){
    const now=clockNow();
    $('#map-rotation').innerHTML=`<div class="map-slot-grid">${modes.map(([key,label])=>panel(key,label,now)).join('')}</div>
      <div class="map-source"><span role="status">${failed?'暂未取到最新轮换，可点击刷新重试。':pending?'正在更新轮换…':''}</span>
      <button class="text-button" data-map-source type="button">轮换来源 · Apex Legends Status</button></div>`;
  }

  async function fetchRotation(){
    if(pending||typeof window.pywebview?.api?.map_rotation!=='function')return;
    pending=true;
    $('#map-refresh').disabled=true;
    try{
      const packet=await call('map_rotation');
      latest={...packet,loaded:performance.now()};
      failed=!!packet.error;
    }catch{
      failed=true;
    }finally{
      nextRequest=performance.now()+45000;
      pending=false;
      $('#map-refresh').disabled=false;
      rankedCaption(clockNow());
      if(!$('#maps-view').hidden)rotation();
    }
  }

  function tick(){
    if(document.hidden)return;
    const mapsVisible=!$('#maps-view').hidden;
    const overviewVisible=!$('#overview-view').hidden;
    const now=clockNow();
    rankedCaption(now);
    if(mapsVisible){
      for(const clock of $$('[data-rotation-end]')){
        const left=Number(clock.dataset.rotationEnd)-now;
        if(left<=0){rotation();break;}
        clock.textContent=remaining(left);
      }
    }
    if(!mapsVisible&&!overviewVisible)return;
    const wanted=mapsVisible?modes.map(([mode])=>mode):['ranked'];
    const due=failed||wanted.some(mode=>!latest?.modes?.[mode]?.current||latest.modes[mode].current.end<=now);
    if(due&&performance.now()>=nextRequest)fetchRotation();
  }

  function render(state){
    const scope=$('#map-scope').value;
    const rows=MatchLogOverview.range(state.matches,'all').rows.filter(match=>scope==='all'||(match.mode||'ranked')===scope);
    const signature=JSON.stringify([scope,state.config.self_name,rows.map(match=>[match.map_name,match.mode,match.summary?.placement,own(match)?.damage,effectiveRp(match)])]);
    if(signature===statsSignature)return;
    statsSignature=signature;
    const grouped=new Map();
    let unknown=0;
    for(const match of rows){
      if(!match.map_name){unknown++;continue;}
      const [name,art]=mapInfo(match.map_name);
      const row=grouped.get(name)||{name,art,games:0,wins:0,places:[],damage:[],rp:[]};
      row.games++;
      const place=match.summary?.placement, damage=own(match)?.damage, delta=effectiveRp(match);
      if(Number.isInteger(place)&&place>=1&&place<=30){row.places.push(place);row.wins+=Number(place===1);}
      if(Number.isFinite(damage))row.damage.push(damage);
      if(Number.isFinite(delta))row.rp.push(delta);
      grouped.set(name,row);
    }
    const all=[...grouped.values()].sort((a,b)=>b.games-a.games||a.name.localeCompare(b.name,'zh-CN'));
    const display=all.map(row=>{
      const rp=row.rp.length?row.rp.reduce((sum,n)=>sum+n,0):null;
      const label=row.art&&document.documentElement?.dataset.artwork!=='public'?`<img src="assets/maps/${row.art}.webp" alt="">`:'';
      return `<tr><th scope="row">${label}${esc(row.name)}</th><td>${row.games}</td>
        <td>${row.places.length?row.wins:'—'}</td><td>${fmt(avg(row.places))}</td><td>${fmt(avg(row.damage))}</td>
        <td class="${tone(rp)}">${signed(rp)}</td></tr>`;
    }).join('');
    $('#map-stats').innerHTML=`<div class="map-stats-caption subtle">${rows.length-unknown} 局有地图${unknown?` · ${unknown} 局未识别地图`:''}</div>`+
      (all.length?`<div class="panel table-panel map-results"><table><thead><tr><th>地图</th><th>对局</th><th>吃鸡</th><th>平均排名</th><th>场均伤害</th><th>净 RP</th></tr></thead><tbody>${display}</tbody></table></div>`:
        '<div class="panel empty-state">这个范围还没有带地图的对局。</div>');
  }

  function enter(state){
    render(state);
    rotation();
    tick();
  }
  function start(){
    $('#map-refresh').onclick=fetchRotation;
    $('#map-scope').onchange=()=>render(state);
    $('#map-rotation').onclick=event=>{
      if(event.target.closest('[data-map-source]'))action(()=>call('open_map_source'));
    };
    window.addEventListener('pywebviewready',tick);
    document.addEventListener('visibilitychange',tick);
    setInterval(tick,1000);
    tick();
  }
  return {start,enter,render,tick,label:name=>mapInfo(name)[0]};
})();
