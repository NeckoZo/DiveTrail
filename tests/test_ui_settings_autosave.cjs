const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');

const app = readFileSync(join(__dirname, '../ui/app.js'), 'utf8');
const autosave = app.slice(app.indexOf('const settingsDrafts='), app.indexOf('function showSettings('));
const settle = () => new Promise(resolve => setImmediate(resolve));

function fixture() {
  const state = {config:{self_name:'Before', low_impact:true, friends:['First','Second','Third'], cleanup_review_days:2}};
  const requests = [];
  const messages = [];
  const status = {classList:{toggle() {}}};
  const form = {};
  const dialog = {open:true, close() {this.open=false;}};
  const controls = {'#settings-save-status':status, '#settings-form':form, '#settings-dialog':dialog};
  const timers = new Map();
  const context = {
    state, $:id => controls[id], norm:value => value.trim().toLowerCase(),
    setTimeout:callback => {
      const id = Symbol();
      timers.set(id, callback);
      return id;
    },
    clearTimeout:id => timers.delete(id),
    applyState:result => Object.assign(state, result), toast:message => messages.push(message),
    call:(_method, patch) => new Promise((resolve, reject) => {
      const saved = JSON.parse(JSON.stringify(patch));
      requests.push({patch:saved, finish:() => resolve({config:{...state.config, ...saved}}), reject});
    }),
  };
  const api = runInNewContext(`const friendFields=['friend_one','friend_two'];\n${autosave}\nsettingsFriends=[...state.config.friends]; ({changeSetting, closeSettings})`, context);
  const control = (name, value, props = {}) => ({name, value, type:'text', checkValidity:() => true, setAttribute() {}, ...props});
  const tick = () => {for (const callback of [...timers.values()]) callback();};
  return {...api, requests, messages, status, dialog, control, tick};
}

test('immediate controls save independently and waiting text writes serialize', async () => {
  const f = fixture();
  const name = f.control('self_name', 'A', {required:true});
  f.changeSetting(name, false);
  f.changeSetting(f.control('low_impact', '', {type:'checkbox', checked:false}), true);
  await settle();
  assert.deepEqual(f.requests.map(r => r.patch), [{low_impact:false}]);

  name.value = 'After';
  f.changeSetting(name, false);
  f.tick();
  await settle();
  assert.equal(f.requests.length, 1);

  f.requests[0].finish();
  await settle();
  assert.deepEqual(f.requests.map(r => r.patch), [{low_impact:false}, {self_name:'After'}]);
  f.requests[1].finish();
  await settle();
  assert.match(f.status.textContent, /已自动保存/);
});

test('close flushes valid text, preserves extra friends, and discards invalid numbers', async () => {
  const f = fixture();
  f.changeSetting(f.control('friend_one', ''), false);
  f.changeSetting(f.control('friend_two', 'Revised'), false);
  f.changeSetting(f.control('cleanup_review_days', '', {
    type:'number', required:true, valueAsNumber:NaN, checkValidity:() => false,
  }), false);
  const closing = f.closeSettings();
  await settle();
  assert.equal(f.dialog.open, true);
  assert.deepEqual(f.requests[0].patch, {friends:['Second','Third']});
  f.requests[0].finish();
  await settle();
  assert.deepEqual(f.requests[1].patch, {friends:['Revised','Third']});
  f.requests[1].finish();
  assert.equal(await closing, true);
  assert.equal(f.dialog.open, false);
  assert.equal(f.requests.length, 2);
  assert.match(f.messages[0], /无效的设置未保存/);
});

test('a failed write remains visible and can be retried when closing', async () => {
  const f = fixture();
  f.changeSetting(f.control('self_name', 'After'), false);
  const closing = f.closeSettings();
  await settle();
  f.requests[0].reject(new Error('测试错误'));
  assert.equal(await closing, false);
  assert.equal(f.dialog.open, true);
  assert.match(f.status.textContent, /测试错误/);
  const retry = f.closeSettings();
  await settle();
  assert.deepEqual(f.requests[1].patch, {self_name:'After'});
  f.requests[1].finish();
  assert.equal(await retry, true);
});
