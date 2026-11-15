const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');

test('slow polling stays single-flight and queued forced refreshes share a fresh response', async () => {
  const app = readFileSync(join(__dirname, '../ui/app.js'), 'utf8');
  const script = app.slice(app.indexOf('function refresh(force'), app.indexOf('const settingsDrafts'));
  const requests = [];
  const refresh = runInNewContext(`
    let refreshInFlight=null,queuedForcedRefresh=null;
    let pendingMutations=0,stateEpoch=0,pollRevision='previous',view='matches';
    ${script}
    refresh
  `, {
    window:{pywebview:{api:{poll_state(){}}}}, document:{hidden:false},
    state:{running:true, runtime:{}}, maybeShowStartupDialogs(){}, console,
    call:(_method, revision) => new Promise(resolve => requests.push({revision, resolve})),
  });
  const first = refresh();
  for(let n=0;n<15;n++)assert.equal(refresh(), first);
  const forced = refresh(true);
  assert.equal(refresh(true), forced);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].revision, 'previous');
  const live = {running:true, runtime:{}};
  requests[0].resolve({revision:'new', live});
  await first;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 2);
  assert.equal(requests[1].revision, null);
  requests[1].resolve({revision:'latest', live});
  await forced;
});
