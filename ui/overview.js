(() => {
  const labels = {'12':'过去 12 小时', '24':'过去 24 小时', '30':'近 30 局', all:'本赛段'};

  function range(matches, value, now = Date.now()) {
    const valid = matches.filter(m =>
      Number.isFinite(m.created) && m.created * 1000 <= now
      && m.status?.code !== 'ongoing' && !(m.record_origin === 'upload' && !m.time_corrected)
    ).sort((a,b) => b.created - a.created);
    const timeWindow = value === '12' || value === '24';
    const start = timeWindow ? now - Number(value) * 3600000 : null;
    const rows = timeWindow ? valid.filter(m => m.created * 1000 >= start)
      : value === '30' ? valid.slice(0,30) : valid;
    return {rows, windowStart:start, start:start ?? (rows.length ? rows.at(-1).created * 1000 : now), label:labels[value]};
  }

  function points(source, scope, split) {
    const ids = new Set(scope.rows.map(row => row.id));
    const selected = [];
    let prior = null;
    for (const point of source) {
      if (point.split != null && String(point.split) !== String(split)) continue;
      if (point.match_id ? ids.has(point.match_id) : point.at * 1000 >= scope.start && (!scope.now || point.at * 1000 <= scope.now)) selected.push(point);
      else if (point.value != null && point.at * 1000 <= scope.start) prior = point;
    }
    return selected.length && prior && prior.at < selected[0].at
      ? [{...prior, match_id:null, label:'范围起点', delta:null, adjustment:null}, ...selected]
      : selected;
  }


  function insights(rows, own, score) {
    const damages = [], rosters = new Map();
    let scored = 0, up = 0, down = 0;
    for (const row of rows) {
      const player = own(row), value = score(row);
      if (Number.isFinite(player?.damage)) damages.push(player.damage);
      if (Number.isFinite(value)) {
        scored++;
        if (value > 0) up++;
        if (value < 0) down++;
      }
      const team = row.summary?.players || [];
      if (player?.hero && team.length === 3 && team.every(person => person.hero)) {
        const names = [player.hero, ...team.filter(person => person !== player).map(person => person.hero).sort()];
        const key = names.join('|');
        const roster = rosters.get(key) || {names, count:0};
        roster.count++;
        rosters.set(key, roster);
      }
    }
    damages.sort((a,b) => a-b);
    const middle = Math.floor(damages.length / 2);
    const median = damages.length
      ? (damages[Math.floor((damages.length - 1) / 2)] + damages[middle]) / 2 : null;
    const roster = [...rosters.values()].sort((a,b) => b.count - a.count)[0];
    return {scored, up, down, median, roster, damageGames:damages.length,
            highDamage:damages.filter(value => value >= 1000).length};
  }

  function trajectory(rows, score) {
    let total = 0;
    return [0, ...[...rows].reverse().map(row => {
      const value = score(row);
      if (!Number.isFinite(value)) return null;
      total += value;
      return total;
    })];
  }

  function drawTrajectory(ctx, values, x, y, w, h) {
    const measured = values.filter(Number.isFinite);
    if (measured.length < 2) return;
    const low = Math.min(...measured), high = Math.max(...measured);
    const padding = Math.max(12,(high-low)*.10);
    const project = (value,i) => [x + w*i/(values.length-1), y + h*(high+padding-value)/(high-low+2*padding)];
    const baseline = project(0,0)[1];
    ctx.strokeStyle = '#414650';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x,baseline);
    ctx.lineTo(x+w,baseline);
    ctx.stroke();
    let previous = 0;
    values.forEach((value,i) => {
      if (!i || !Number.isFinite(value)) return;
      ctx.beginPath();
      ctx.lineWidth = 3;
      ctx.strokeStyle = value < values[previous] ? '#ff9890' : '#77dcb2';
      ctx.setLineDash(i - previous > 1 ? [4,6] : []);
      ctx.moveTo(...project(values[previous],previous));
      ctx.lineTo(...project(value,i));
      ctx.stroke();
      previous = i;
    });
    ctx.setLineDash([]);
    const [lastX,lastY] = project(values[previous],previous);
    ctx.beginPath();
    ctx.fillStyle = '#ecebf0';
    ctx.arc(lastX,lastY,4,0,Math.PI*2);
    ctx.fill();
  }

  function playedTime(rows, windowStart = null, now = Date.now()) {
    const stamp = value => typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1)
      : typeof value === 'string' ? Date.parse(value) : NaN;
    const intervals = [];
    for (const row of rows) {
      if (row.time_corrected) continue;
      const started = stamp(row.round_started_at);
      const ended = stamp(row.round_end_at ?? row.settled_at ?? row.snapshot_at);
      if (!(ended > started && ended - started <= 3600000)) continue;
      const start = Math.max(started, windowStart ?? started);
      const end = Math.min(ended, now);
      if (end > start) intervals.push([start, end]);
    }
    intervals.sort((a, b) => a[0] - b[0]);
    let milliseconds = 0, lastEnd = -Infinity;
    for (const [start, end] of intervals) {
      milliseconds += Math.max(0, end - Math.max(start, lastEnd));
      lastEnd = Math.max(lastEnd, end);
    }
    return {milliseconds, games:intervals.length};
  }

  function share(canvas, scope, owner, hideNames, values, art) {
    canvas.width = 960;
    canvas.height = 520;
    const ctx = canvas.getContext('2d');
    const fill = (color,x,y,w,h) => { ctx.fillStyle=color; ctx.fillRect(x,y,w,h); };
    const write = (text,x,y,size,color='#f2f1f3',weight=400,maxWidth=800) => {
      ctx.font = weight+' '+size+'px "Segoe UI", "Microsoft YaHei UI", sans-serif';
      ctx.fillStyle = color;
      ctx.fillText(text,x,y,maxWidth);
    };
    const muted = '#b9bac5', coral = '#fa917a';
    fill('#15161b',0,0,960,520);
    fill(coral,0,0,960,4);
    const backdrop = ctx.createLinearGradient(575,26,920,315);
    backdrop.addColorStop(0,'#282230');
    backdrop.addColorStop(1,'#432922');
    fill(backdrop,572,24,352,300);
    const best = scope.best;
    const picture=art?.image;
    if (picture?.naturalWidth && picture.naturalHeight) {
      const [left,top,right,bottom] = art.bounds ?? [0,0,picture.naturalWidth,picture.naturalHeight];
      const width=right-left, height=bottom-top;
      const scale=Math.min(327/width,289/height);
      const w=width*scale, h=height*scale;
      ctx.drawImage(picture,left,top,width,height,748-w/2,324-h,w,h);
    } else {
      write(best?.hero || '—',598,183,40,'#e6d3ce',650,298);
    }

    write('DIVETRAIL · 尾迹',40,53,16,coral,700);
    write('排位战绩',38,116,46,'#f2f1f3',750);
    const played = playedTime(scope.rows, scope.windowStart, scope.now);
    const minutes = Math.floor(played.milliseconds / 60000);
    const duration = !played.games ? '时长未记录' : !minutes ? '不足 1 分钟'
      : minutes < 60 ? minutes+' 分钟' : Math.floor(minutes/60)+' 小时 '+minutes%60+' 分钟';
    write([played.games?'对局 '+duration:duration,hideNames?'':owner].filter(Boolean).join(' · '),41,151,20,muted,500,495);

    const score = scope.net == null ? '— RP' : (scope.net>=0?'+':'')+scope.net.toLocaleString('zh-CN')+' RP';
    write(score,36,241,71,scope.net == null ? muted : scope.net<0?'#ff9890':'#77dcb2',750,296);
    const coverage = played.games > 0 && played.games < scope.rows.length ? ' · '+played.games+' 局计入时长' : '';
    write(scope.rows.length+' 局'+coverage,41,279,19,muted,500,296);

    const stats=scope.shareStats;
    if(stats && scope.rows.length){
      fill('#3a3b44',355,169,1,112);
      const list=[
        ['吃鸡',stats.placementGames?stats.wins+' 场':'—'],
        ['前五',stats.placementGames?stats.topFive+' 场':'—'],
        ['场均伤害',stats.damage==null?'—':stats.damage.toLocaleString('zh-CN',{maximumFractionDigits:1})],
      ];
      list.forEach(([label,value],index)=>{
        const y=193+index*38;
        write(label,374,y,17,muted,500);
        ctx.textAlign='right';
        write(value,542,y,22,'#f2f1f3',650);
        ctx.textAlign='left';
        if(index<list.length-1)fill('#30313b',374,y+11,168,1);
      });
    }

    fill('#202229',36,313,510,172);
    write('净 RP 走势',56,346,19,'#f2f1f3',650);
    if (values.filter(Number.isFinite).length>1) drawTrajectory(ctx,values,59,365,462,98);
    else write('暂无可用计分',59,415,17,muted);

    fill('#262329',572,324,352,161);
    write('最佳表现',592,354,16,coral,650);
    write(best ? best.damage.toLocaleString('zh-CN') : '—',590,412,45,'#f2f1f3',750,170);
    write(best?.hero || '英雄待识别',773,390,20,'#f2f1f3',650,126);
    write(best?.placement ? '第 '+best.placement+' 名' : '排名未记录',774,416,16,muted,500,126);
    if (best && [best.kills,best.assists,best.knocks].some(Number.isFinite)) {
      write('击杀 / 助攻 / 击倒   '+[best.kills,best.assists,best.knocks].map(v=>v??'—').join(' / '),592,453,17,muted,500,312);
    }
    return canvas;
  }

  const api = {range, points, insights, trajectory, playedTime, share};
  if (typeof module !== 'undefined') module.exports = api;
  else window.MatchLogOverview = api;
})();
