const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');

const app = readFileSync(join(__dirname, '../ui/app.js'), 'utf8');
const functions = app.slice(app.indexOf('function mergeError('), app.indexOf('function clearMatchFilters('));
const handlers = app.slice(app.indexOf("$('#merge-first').onchange=renderMergeCandidates"), app.indexOf("$('#undo-merge-btn').onclick"));

function fixture() {
  const controls = {};
  for (const id of ['merge-submit', 'merge-form', 'merge-error', 'merge-first', 'merge-second',
      'merge-selection', 'merge-edit-selection', 'merge-preview', 'merge-dialog', 'merge-preview .merge-targets']) {
    controls['#'+id] = {};
  }
  const state = {matches:[{id:'left'}, {id:'right'}]};
  const pending = [];
  const dialog = controls['#merge-dialog'];
  const events = {};
  dialog.querySelectorAll = () => [];
  dialog.addEventListener = (event, fn) => { events[event] = fn; };
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; events.close(); };
  controls['#merge-form'].setAttribute = () => {};
  let previewHtml = '';
  Object.defineProperty(controls['#merge-preview'], 'innerHTML', {set(html) {
    previewHtml = html;
    if (html.includes('id="merge-confirm-uncertain"')) controls['#merge-confirm-uncertain'] = {checked:false};
    else delete controls['#merge-confirm-uncertain'];
  }});
  controls['#merge-preview input[name="merge-target"]:checked'] = {value:'left'};
  const api = runInNewContext(`
    let mergeDraft=null,mergeBusy=null,mergeRevision=0;
    ${functions}
    ${handlers}
    ({showMergeDialog, submit: () => $('#merge-form').onsubmit({preventDefault(){}})})
  `, {
    state, $: selector => controls[selector], esc:String, statusLabel:()=>'待核对',
    mergeRow: match => ({label:match.id,pages:''}), applyState(){}, toast(){},
    call: (method, ...args) => new Promise((resolve,reject) => pending.push({method,args,resolve,reject})),
  });
  function open() {
    api.showMergeDialog('left');
    controls['#merge-second'].value='right';
  }
  return {...api, open, pending, dialog, controls, preview:() => previewHtml};
}

test('uncertain merge requires acknowledgement and restores the controls after failure', async () => {
  const f = fixture();
  f.open();
  const preview = f.submit();
  assert.equal(f.controls['#merge-submit'].textContent,'正在核对…');
  f.pending[0].resolve({token:'first',warning:'请核对场次'});
  await preview;
  assert.equal(f.controls['#merge-submit'].disabled,true);
  const ack = f.controls['#merge-confirm-uncertain'];
  ack.checked=true;
  ack.onchange();
  assert.equal(f.controls['#merge-submit'].disabled,false);
  const merge = f.submit();
  assert.equal(f.controls['#merge-submit'].textContent,'正在合并…');
  assert.equal(f.pending[1].args[2],true);
  f.pending[1].reject(new Error('队列繁忙'));
  await merge;
  assert.equal(f.controls['#merge-error'].textContent,'队列繁忙');
  assert.equal(f.controls['#merge-submit'].disabled,false);
  assert.equal(f.controls['#merge-submit'].textContent,'确认合并');
});

test('a closed preview cannot overwrite the reopened merge dialog', async () => {
  const f = fixture();
  f.open();
  const first = f.submit();
  f.dialog.close();
  f.open();
  const second = f.submit();
  f.pending[0].resolve({token:'old',warning:'不应出现'});
  await first;
  assert.equal(f.preview(),'');
  assert.equal(f.controls['#merge-submit'].textContent,'正在核对…');
  f.pending[1].resolve({blocked:true,conflicts:['已有两份不同结算']});
  await second;
  assert.match(f.preview(),/已有两份不同结算/);
  assert.equal(f.controls['#merge-submit'].textContent,'暂不可合并');
  assert.equal(f.controls['#merge-submit'].disabled,true);
});
