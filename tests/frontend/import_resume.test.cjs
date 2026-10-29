const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/resources/js/import.js', 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness({ storage = new Map(), requests = [], status, upload, base = 'http://127.0.0.1:8010', blockedStorage = false } = {}) {
  const nodes = new Map(), timers = new Map(), windowEvents = {}, toasts = [];
  let nextTimer = 1;
  function element(tag = 'div', className = '', text = '') {
    const el = { tag, className: className || '', textContent: text, children: [], style: {}, dataset: {}, listeners: {}, value: '',
      appendChild(child) { child.parent = this; this.children.push(child); return child; },
      remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); },
      addEventListener(type, fn) { this.listeners[type] = fn; }, focus() {},
      querySelector(selector) {
        if (this.selectors && this.selectors[selector]) return this.selectors[selector];
        return this.children.find(child => child.className.split(/\s+/).includes(selector.slice(1))) || null;
      },
    };
    Object.defineProperty(el, 'innerHTML', { set(value) {
      this.children = [];
      if (String(value).includes('empty-hint')) this.appendChild(element('div', 'empty-hint', '暂无导入任务'));
    } });
    el.classList = { add(name) { el.className += ' ' + name; }, remove(name) { el.className = el.className.split(/\s+/).filter(value => value !== name).join(' '); } };
    return el;
  }
  ['dropZone', 'fileInput', 'fileList', 'importToken', 'saveToken', 'documentKind', 'documentVersion', 'policyUntil'].forEach(id => {
    const node = element(); node.id = id; nodes.set(id, node);
  });
  nodes.get('documentKind').value = 'manual';
  const fileList = nodes.get('fileList');
  fileList.insertAdjacentHTML = (_, html) => {
    const item = element('div', 'file-item');
    item.id = /id="([^"]+)"/.exec(html)[1]; item.html = html;
    item.selectors = {};
    ['.file-name', '.file-size', '.progress-bar-container', '.progress-bar', '.log-details', '.log-details summary', '.log-list', '.status-badge'].forEach(selector => {
      item.selectors[selector] = element('div', selector.slice(1));
    });
    nodes.set(item.id, item); item.parent = fileList; fileList.children.unshift(item);
  };
  const context = {
    App: { base, configure() {}, getImportToken: () => 'test-token', setImportToken() {}, toast: text => toasts.push(text),
      escapeHtml: value => String(value).replace(/</g, '&lt;'), create: element,
      PATHS: { upload: '/upload', taskStatus: id => '/status/' + encodeURIComponent(id) },
      fetchJson: async (path, options) => {
        requests.push(path);
        if (path === '/upload') return upload ? upload() : { task_ids: ['task-1'] };
        return status ? status(path) : { status: 'processing', done_list: ['上传文件'], running_list: ['生成向量'] };
      },
    },
    document: { getElementById: id => nodes.get(id) },
    sessionStorage: { getItem(key) { if (blockedStorage) throw Error('blocked'); return storage.get(key) || null; },
      setItem(key, value) { if (blockedStorage) throw Error('blocked'); storage.set(key, value); } },
    FormData: class { append() {} }, console: { warn() {}, error() {} },
    window: { addEventListener: (type, callback) => { windowEvents[type] = callback; } },
    setInterval: callback => { const id = nextTimer++; timers.set(id, callback); return id; },
    clearInterval: id => timers.delete(id),
  };
  vm.runInNewContext(source, context);
  return { storage, requests, nodes, timers, toasts, fileList,
    cards: () => fileList.children.filter(child => child.className === 'file-item'),
    upload: async () => { nodes.get('fileInput').listeners.change({ target: { files: [{ name: 'manual.md', size: 128 }] } }); await flush(); },
    tick: async () => { await Promise.all([...timers.values()].map(callback => callback())); await flush(); },
    hide: () => windowEvents.pagehide(), show: () => windowEvents.pageshow({ persisted: true }),
    saveToken: () => nodes.get('saveToken').listeners.click(),
  };
}

test('refresh restores a running import and polls the existing task without uploading again', async () => {
  const first = harness(); await first.upload(); await first.tick(); first.hide();
  const second = harness({ storage: first.storage, requests: first.requests }); await flush();
  assert.equal(second.cards().length, 1);
  assert.match(second.cards()[0].html, /manual\.md/);
  assert.match(second.cards()[0].querySelector('.status-badge').textContent, /处理中/);
  assert.equal(second.requests.filter(path => path === '/upload').length, 1);
  assert.ok(second.requests.includes('/status/task-1'));
  assert.equal(second.timers.size, 1);
});

for (const state of ['completed', 'failed']) {
  test('refresh keeps a terminal import and its result: ' + state, async () => {
    const status = () => ({ status: state, done_list: ['上传文件', '写入索引'], running_list: [], error_code: state === 'failed' ? 'import_failed' : null, message: '测试失败原因' });
    const first = harness({ status }); await first.upload(); await first.tick(); first.hide();
    const second = harness({ storage: first.storage, requests: first.requests, status }); await flush();
    assert.equal(second.cards().length, 1);
    assert.match(second.cards()[0].querySelector('.status-badge').textContent, state === 'completed' ? /已完成/ : /失败/);
    assert.equal(second.timers.size, 0);
    if (state === 'failed') assert.match(second.cards()[0].querySelector('.log-list').children[0].textContent, /测试失败原因/);
  });
}

test('refresh during an unfinished upload shows interruption and does not invent a server task', async () => {
  const first = harness({ upload: () => new Promise(() => {}) }); await first.upload(); first.hide();
  const second = harness({ storage: first.storage, requests: first.requests }); await flush();
  assert.equal(second.cards().length, 1);
  assert.match(second.cards()[0].querySelector('.status-badge').textContent, /上传中断/);
  assert.match(second.cards()[0].querySelector('.log-list').children[0].textContent, /重新选择文件/);
  assert.equal(second.requests.filter(path => path.startsWith('/status/')).length, 0);
  assert.equal(second.requests.filter(path => path === '/upload').length, 1);
});

test('saving a valid token resumes restored tasks after an authorization failure', async () => {
  const first = harness(); await first.upload(); first.hide();
  let authorized = false;
  const second = harness({ storage: first.storage, status: () => {
    if (!authorized) throw Object.assign(Error('unauthorized'), { status: 401 });
    return { status: 'processing', done_list: [], running_list: ['解析'] };
  } });
  await flush(); assert.equal(second.timers.size, 0);
  authorized = true; second.saveToken(); await flush();
  assert.match(second.cards()[0].querySelector('.status-badge').textContent, /处理中/);
  assert.equal(second.timers.size, 1);
});

test('saving a token replaces an in-flight poll and ignores its later authorization failure', async () => {
  let rejectOld;
  let calls = 0;
  const h = harness({ status: () => ++calls === 1 ? new Promise((_, reject) => { rejectOld = reject; })
    : { status: 'processing', done_list: ['上传文件'], running_list: ['解析'] } });
  await h.upload();
  h.saveToken(); await flush();
  assert.equal(calls, 2);
  rejectOld(Object.assign(Error('unauthorized'), { status: 401 })); await flush();
  assert.match(h.cards()[0].querySelector('.status-badge').textContent, /处理中/);
  assert.equal(h.timers.size, 1);
});

for (const state of ['completed', 'failed']) {
  test('a cached terminal result survives refresh when its server snapshot has expired: ' + state, async () => {
    const first = harness({ status: () => ({ status: state, done_list: ['写入索引'], running_list: [],
      error_code: state === 'failed' ? 'import_failed' : '', message: '保留的失败原因' }) });
    await first.upload(); first.hide();
    const second = harness({ storage: first.storage, status: () => ({ status: '', done_list: [], running_list: [] }) });
    await flush(); second.saveToken(); second.show(); await flush();
    assert.match(second.cards()[0].querySelector('.status-badge').textContent, state === 'completed' ? /已完成/ : /失败/);
    assert.equal(second.requests.length, 0);
    assert.equal(second.timers.size, 0);
    const cached = JSON.parse([...second.storage.values()][0]);
    assert.equal(cached[0].status, state);
    if (state === 'failed') assert.match(second.cards()[0].querySelector('.log-list').children[0].textContent, /保留的失败原因/);
  });
}

test('storage failures do not prevent an upload, and another API never inherits its task list', async () => {
  const blocked = harness({ blockedStorage: true }); await blocked.upload();
  assert.ok(blocked.requests.includes('/upload'));
  const first = harness(); await first.upload(); first.hide();
  const other = harness({ storage: first.storage, base: 'http://127.0.0.1:9010' }); await flush();
  assert.equal(other.cards().length, 0);
  assert.equal(other.requests.length, 0);
});

test('a temporary status failure keeps the task available for the next poll', async () => {
  let failing = true;
  const h = harness({ status: () => {
    if (failing) throw Error('network');
    return { status: 'completed', done_list: ['写入索引'], running_list: [] };
  } });
  await h.upload(); await h.tick();
  assert.equal(h.timers.size, 1);
  failing = false; await h.tick();
  assert.match(h.cards()[0].querySelector('.status-badge').textContent, /已完成/);
  assert.equal(h.timers.size, 0);
});

test('a temporary HTTP 503 keeps polling instead of asking for another token', async () => {
  let failing = true;
  const h = harness({ status: () => {
    if (failing) throw Object.assign(Error('unavailable'), { status: 503, code: 'service_unavailable' });
    return { status: 'completed', done_list: ['写入索引'], running_list: [] };
  } });
  await h.upload();
  assert.equal(h.timers.size, 1);
  assert.match(h.cards()[0].querySelector('.status-badge').textContent, /正在重试/);
  failing = false; await h.tick();
  assert.match(h.cards()[0].querySelector('.status-badge').textContent, /已完成/);
});

test('a missing import token configuration stops polling until the token is saved again', async () => {
  const h = harness({ status: () => { throw Object.assign(Error('missing'), { status: 503, code: 'import_admin_token_missing' }); } });
  await h.upload();
  assert.equal(h.timers.size, 0);
  assert.match(h.cards()[0].querySelector('.status-badge').textContent, /等待验证导入令牌/);
});

test('an old polling response cannot overwrite the resumed page or remove its new timer', async () => {
  let resolveOld;
  let calls = 0;
  const h = harness({ status: () => ++calls === 1 ? new Promise(resolve => { resolveOld = resolve; })
    : { status: 'processing', done_list: [], running_list: ['生成向量'] } });
  await h.upload(); h.hide(); h.show(); await flush();
  resolveOld({ status: 'completed', done_list: [], running_list: [] }); await flush();
  assert.match(h.cards()[0].querySelector('.status-badge').textContent, /处理中/);
  assert.equal(h.timers.size, 1);
});
