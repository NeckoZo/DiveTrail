const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { runInNewContext } = require('node:vm');

const app = readFileSync(join(__dirname, '../ui/app.js'), 'utf8');
const diagnostics = app.slice(app.indexOf('let diagnosticsToken='), app.indexOf('let anonymousToken='));
const html = readFileSync(join(__dirname, '../ui/index.html'), 'utf8');

function setup(call, scope = '24h') {
  const controls = new Map();
  const select = id => {
    if (!controls.has(id)) controls.set(id, {open:true, value:id.endsWith('input:checked')?scope:'', disabled:false});
    return controls.get(id);
  };
  runInNewContext(diagnostics, {
    $:select, call, fmt:String, action:fn=>fn(), refreshCaptureDiagnostics:()=>{}, diagnosticsLoad:0,
  });
  return {
    submit:select('#diagnostics-submit'), status:select('#diagnostics-upload-status'),
    preview:select('#diagnostics-preview'), feedback:select('#diagnostics-feedback'),
    output:select('#capture-diagnostics-text'),
  };
}

test('anonymous diagnostics can be submitted directly and retry uses the same token', async () => {
  assert.doesNotMatch(html.match(/<button[^>]+id="diagnostics-submit"[^>]*>/)[0], /disabled/);
  const calls = [];
  let firstUpload = true;
  const {submit, status} = setup(async (...args) => {
    calls.push(args);
    const [name] = args;
    if (name === 'preview_diagnostics_upload') return {token:'local-report'};
    if (firstUpload) {
      firstUpload = false;
      throw new Error('网络连接失败');
    }
    return {count:4, receipt:'cloud-receipt'};
  }, 'all');

  assert.equal(calls.length, 0);
  await submit.onclick();
  assert.match(status.textContent, /网络连接失败/);
  assert.equal(submit.disabled, false);
  await submit.onclick();
  assert.deepEqual(calls, [
    ['preview_diagnostics_upload', 'all', ''],
    ['upload_diagnostics', 'local-report'],
    ['upload_diagnostics', 'local-report'],
  ]);
  assert.match(status.textContent, /已提交 4 条日志 · 回执 cloud-receipt/);
});

test('empty range does not send anonymous diagnostics', async () => {
  const calls = [];
  const {submit, status} = setup(async (...args) => {
    calls.push(args);
    return {count:0, token:null};
  });
  await submit.onclick();
  assert.deepEqual(calls, [['preview_diagnostics_upload', '24h', '']]);
  assert.match(status.textContent, /没有日志/);
  assert.equal(submit.disabled, false);
});

test('text-only preview remains local and editing invalidates its submission token', async () => {
  const calls = [];
  const {feedback, preview, submit, output, status} = setup(async (...args) => {
    calls.push(args);
    const [name,,text] = args;
    return name==='preview_diagnostics_upload'
      ? {token:text, feedback:text, text:'', count:0, shown:0}
      : {count:0, receipt:'test-receipt'};
  }, 'none');
  feedback.value='旧说明';
  await preview.onclick();
  assert.match(output.value, /旧说明/);
  assert.match(status.textContent, /待提交反馈说明/);
  assert.deepEqual(calls, [['preview_diagnostics_upload', 'none', '旧说明']]);
  feedback.value='新说明';
  await feedback.oninput();
  await submit.onclick();
  assert.deepEqual(calls.slice(1), [
    ['preview_diagnostics_upload', 'none', '新说明'], ['upload_diagnostics', '新说明'],
  ]);
  assert.match(status.textContent, /已提交反馈说明 · 回执 test-receipt/);
  assert.equal(feedback.value, '');
});
