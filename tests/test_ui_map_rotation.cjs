const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');

test('home fetches ranked rotation and shares the in-flight result with maps', async () => {
  const script = readFileSync(join(__dirname, '../ui/maps.js'), 'utf8');
  const elements = {
    '#ranked-map-caption': {},
    '#map-rotation': {},
    '#map-refresh': {},
    '#map-scope': {value:'all'},
    '#map-stats': {},
    '#overview-view': {hidden:false},
    '#maps-view': {hidden:true},
  };
  let elapsed = 0;
  const requests = [];
  const events = {};
  const view = runInNewContext(script + '\nMapView', {
    $: selector => elements[selector],
    $$: () => [],
    esc: text => text,
    window: {
      pywebview: {api:{map_rotation(){}}},
      addEventListener: (name, fn) => events[name] = fn,
    },
    document: {hidden:false, addEventListener(){}},
    performance: {now:() => elapsed},
    setInterval(){},
    MatchLogOverview: {range:() => ({rows:[]})},
    call: () => new Promise(resolve => requests.push(resolve)),
  });
  assert.equal(requests.length, 0);
  view.start();
  assert.equal(requests.length, 1);
  events.pywebviewready();
  elements['#maps-view'].hidden = false;
  elements['#overview-view'].hidden = true;
  view.enter({config:{self_name:'Probe'},matches:[]});
  assert.equal(requests.length, 1);
  requests[0]({
    now:100,
    modes:{
      ranked:{
        current:{map:'Storm Point',start:60,end:160},
        next:{map:'E-District',start:160,end:220},
      },
      mixtape:{
        current:{map:'Barometer',event:'Control',start:60,end:160},
        next:{map:'Fragment',event:'TDM',start:160,end:220},
      },
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements['#ranked-map-caption'].textContent, '排位地图 · 风暴点 · 剩余 1 分钟');
  assert.match(elements['#map-rotation'].innerHTML, /风暴点/);
  assert.match(elements['#map-rotation'].innerHTML, /晴雨表 · 控制/);
  assert.match(elements['#map-rotation'].innerHTML, /碎片 · 团队死斗/);

  elements['#maps-view'].hidden = true;
  elements['#overview-view'].hidden = false;
  elapsed = 60_000;
  view.tick();
  view.tick();
  assert.equal(elements['#ranked-map-caption'].textContent, '排位地图 · 电力区域 · 剩余 1 分钟');
  assert.equal(requests.length, 2);
});
