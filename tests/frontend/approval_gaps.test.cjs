const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function page(items, issueItems = []) {
  const elements = new Map();
  function element(tag) {
    return { tag, children: [], listeners: {}, textContent: '', value: '',
      appendChild(child) { this.children.push(child); return child; },
      addEventListener(name, fn) { this.listeners[name] = fn; },
      querySelector() { return element('div'); }
    };
  }
  ['list', 'count', 'statusFilter', 'adminToken', 'evoStatus', 'refresh', 'gapList', 'issueList', 'issueStatusFilter'].forEach(id => elements.set(id, element('div')));
  elements.get('issueStatusFilter').value = 'unresolved';
  const requests = [];
  const App = {
    configure() {}, getAdminToken: () => 'admin', setAdminToken() {},
    escapeHtml: value => String(value || ''), formatDateTime: () => 'now', toast() {},
    confirm: async () => true,
    create(tag, cls, text) { const el = element(tag); el.className = cls; el.textContent = text || ''; return el; },
    PATHS: { candidates: '/api/evolution/candidates', evolutionStatus: '/api/evolution/status' },
    async fetchJson(path, options) {
      requests.push({ path, options });
      if (path === '/api/evolution/gaps') return { items };
      if (path === '/api/evolution/candidates') return { items: [] };
      if (path === '/api/evolution/status') return { enabled: true };
      if (path === '/api/evolution/trial-metrics') return { routes: {} };
      if (path.startsWith('/api/evolution/issues')) return { items: issueItems };
      return { items: [] };
    }
  };
  vm.runInNewContext(fs.readFileSync('app/resources/js/approval.js', 'utf8'), {
    App, document: { getElementById: id => elements.get(id), createElement: element }
  });
  return { elements, requests };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
function descendants(el) { return [el, ...el.children.flatMap(descendants)]; }

test('approval page displays a scanned gap and its skip reason even with no candidates', async () => {
  const h = page([{ id: 'g1', query: 'HAK180功率？', gap_kind: 'reading_miss', status: 'strategy_pending',
    processing_reason: '阅读遗漏：已转策略修复' }]);
  await tick();
  const nodes = descendants(h.elements.get('gapList'));
  assert.ok(nodes.some(node => node.textContent.includes('HAK180功率')));
  assert.ok(nodes.some(node => node.textContent.includes('已转策略修复')));
  const reason = nodes.find(node => node.tag === 'input');
  reason.value = '已核对原手册，需要补资料';
  const confirm = nodes.find(node => node.tag === 'button' && node.textContent === '确认为知识缺失');
  await confirm.listeners.click();
  const request = h.requests.find(row => row.path === '/api/evolution/gaps/g1/review');
  assert.equal(request.options.method, 'POST');
  assert.deepEqual(JSON.parse(request.options.body), { disposition: 'knowledge_missing', reason: '已核对原手册，需要补资料' });
});

test('dismissed greeting is visible but cannot be turned into a knowledge candidate', async () => {
  const h = page([{ id: 'g2', query: '你好', status: 'dismissed', processing_reason: '历史纯寒暄误判' }]);
  await tick();
  const nodes = descendants(h.elements.get('gapList'));
  assert.ok(nodes.some(node => node.textContent.includes('历史纯寒暄误判')));
  assert.equal(nodes.filter(node => node.tag === 'button').length, 0);
});

test('one work item shows all reasons, source count and earlier handling notes', async () => {
  const h = page([], [{ id: 'i1', query: '烫金机有蓝牙吗', status: 'in_progress',
    item_names: ['HAK180'], issue_types: ['missing_material', 'reading_failure'], record_count: 3,
    owner: '员工A', related_records: [{ issue_type: 'reading_failure', resolution: '已检查手册' }] }]);
  await tick();
  const nodes = descendants(h.elements.get('issueList'));
  assert.ok(nodes.some(node => node.textContent.includes('缺资料') && node.textContent.includes('阅读遗漏')));
  assert.ok(nodes.some(node => node.textContent.includes('3条')));
  assert.ok(nodes.some(node => node.textContent.includes('已检查手册')));
  assert.ok(h.requests.some(row => row.path === '/api/evolution/issues?status=unresolved'));
  const save = nodes.find(node => node.tag === 'button' && node.textContent === '保存整组处理记录');
  assert.ok(save);
  await save.listeners.click();
  assert.equal(h.requests.find(row => row.path === '/api/evolution/issues/i1').options.method, 'POST');
});

test('resolved work items are available through their own filter', async () => {
  const h = page([]);
  await tick();
  const filter = h.elements.get('issueStatusFilter');
  filter.value = 'resolved';
  assert.ok(filter.listeners.change);
  await filter.listeners.change();
  assert.ok(h.requests.some(row => row.path === '/api/evolution/issues?status=resolved'));
});

test('completed work item preserves result without editable controls', async () => {
  const h = page([], [{ id: 'done', query: '问题', status: 'resolved', owner: '员工B',
    resolution: '已完成验证', record_count: 1, issue_types: ['reading_failure'] }]);
  await tick();
  const nodes = descendants(h.elements.get('issueList'));
  assert.ok(nodes.some(node => node.textContent.includes('已完成验证')));
  assert.equal(nodes.filter(node => ['input', 'textarea', 'button'].includes(node.tag)).length, 0);
});

test('ignored work item explains the manual decision and remains read only', async () => {
  const h = page([], [{ id: 'ignored', query: '烫金机有蓝牙吗', status: 'dismissed',
    suppression_reason: '不用', dismissed_by_gap_id: 'gap1', record_count: 1,
    issue_types: ['missing_material'], item_names: ['HAK180'] }]);
  await tick();
  const nodes = descendants(h.elements.get('issueList'));
  assert.ok(nodes.some(node => node.textContent.includes('已忽略') && node.textContent.includes('不用')));
  assert.equal(nodes.filter(node => ['input', 'textarea', 'button'].includes(node.tag)).length, 0);
  const filter = h.elements.get('issueStatusFilter');
  filter.value = 'dismissed';
  await filter.listeners.change();
  assert.ok(h.requests.some(row => row.path === '/api/evolution/issues?status=dismissed'));
});
