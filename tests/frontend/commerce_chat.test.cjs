const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element(tagName, className = '', textContent = '') {
  return {
    tagName, className, textContent, children: [], dataset: {}, listeners: {}, disabled: false, value: '', attributes: {},
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.append(child); return child; },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    focus() {},
    remove() { this.hidden = true; },
    addEventListener(event, callback) { this.listeners[event] = callback; },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) {
      const children = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
      return children.filter(node => selector === '*' || node.tagName === selector ||
        (node.className || '').split(' ').includes(selector.slice(1)));
    }
  };
}

const sessionId = 'session-4';
const scope = 'account-a';
const storageKey = `kb_commerce_confirmations:${sessionId}:${scope}`;
const confirmationId = 'a1b2c3';
const orderId = 'order-41';
const productId = 'product-17';
const taskPath = `/api/commerce/task?session_id=${sessionId}`;
const confirmationPath = `/api/commerce/confirmations/${confirmationId}`;
const recentObjects = {
  orders: [{ order_id: orderId, title: '伺服电机', status: 'paid', amount: '125.00',
    created_at: '2030-01-01T08:00:00Z', cover_url: null }],
  products: [{ product_id: productId, title: '电源适配器', price: '99.00', cover_url: null }],
};
const confirmation = {
  confirmation_id: confirmationId, operation: 'refund_application', order_id: orderId,
  status: 'unknown', retryable: true, expires_at: '2030-01-01T09:00:00Z', retry_until: '2030-01-01T10:00:00Z',
  preview: { order_id: orderId, status: 'paid', status_desc: '已支付', amount: '125.00',
    items: [{ title: '样品', quantity: 1, price: '125.00' }], reason: '拍错商品' },
};

function harness(respond, { enabled = true, stored = null, withWorkspace = false } = {}) {
  const source = fs.readFileSync('app/resources/js/chat-commerce.js', 'utf8');
  const calls = [];
  const messages = [];
  const storage = new Map(stored ? [[storageKey, JSON.stringify([stored])]] : []);
  const workspace = withWorkspace ? element('details') : null;
  if (workspace) {
    workspace.hidden = true;
    const summary = element('summary');
    summary.append(element('span', 'commerce-workspace-state'));
    const body = element('div', 'commerce-workspace-body');
    const loginForm = element('form', 'commerce-login-form');
    loginForm.hidden = true;
    loginForm.append(
      element('input', 'commerce-account-id'),
      element('input', 'commerce-account-password'),
      element('button', 'commerce-login-submit', '登录商城'),
    );
    const logout = element('button', 'commerce-logout', '退出商城');
    logout.hidden = true;
    body.append(
      element('button', 'commerce-reload', '刷新'),
      logout,
      loginForm,
      element('p', 'commerce-workspace-status'),
      element('div', 'commerce-workspace-tasks'),
      element('div', 'commerce-workspace-objects'),
    );
    workspace.append(summary, body);
  }
  const listeners = {};
  const sandbox = {
    window: { addEventListener: (event, handler) => { listeners[event] = handler; } }, URL,
    document: { addEventListener: (event, handler) => { listeners[event] = handler; } },
    App: {
      create: element,
      formatDateTime: value => value,
      fetchJson: async (path, options) => {
        const call = { path, method: options.method || 'GET', body: options.body && JSON.parse(options.body) };
        calls.push(call);
        assert.equal(options.token, '');
        const result = await respond(call);
        if (result !== undefined) return result;
        if (path === '/api/commerce/session?include_scope=true') return { authenticated: true, account_scope: scope };
        if (path === '/api/commerce/objects') return recentObjects;
        if (path === taskPath) return { active: null, paused: null };
        assert.fail('Unexpected request ' + path);
      }
    },
    sessionStorage: {
      getItem: key => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    }
  };
  vm.runInNewContext(source, sandbox);
  function addMessage() {
    const message = element('div');
    message.append(element('div', 'bubble'));
    messages.push(message);
    return message;
  }
  const knowledge = [];
  const selectionChanges = [];
  const commerce = sandbox.window.createCommerceChat({
    enabled, sessionId, workspace, addMessage, askKnowledge: query => Boolean(knowledge.push(query)), scrollToBottom() {},
    onSelectionChange: selection => selectionChanges.push(selection)
  });
  return { commerce, calls, messages, addMessage, storage, knowledge, workspace, listeners, selectionChanges };
}

const postBodies = h => h.calls.filter(call => call.method === 'POST').map(call => call.body);
const textOf = node => [node.textContent, ...node.querySelectorAll('*').map(child => child.textContent)].join(' ');
const findButton = (node, text) => node.querySelectorAll('button').find(button => textOf(button).includes(text));

test('disabled chat continues through the existing knowledge path without a commerce request', async () => {
  const h = harness(() => assert.fail('unexpected request'), { enabled: false, stored: confirmationId });
  assert.equal(await h.commerce.assist(h.addMessage(), '怎么安装'), false);
  await h.commerce.restore();
  assert.deepEqual(h.calls, []);
});

test('commerce forwards only the explicit knowledge marker and renders values as text', async () => {
  const title = '<img src=x onerror=alert(1)>';
  const h = harness(({ body }) => body.query === '怎么安装' ? { type: 'knowledge' } : ({
    type: 'result', operation: 'product_detail', result: {
      product_id: 'prod-7', title, price: '9007199254740993.00', cover_url: 'javascript:alert(1)'
    }
  }));
  assert.equal(await h.commerce.assist(h.addMessage(), '怎么安装'), false);
  const msg = h.addMessage();
  assert.equal(await h.commerce.assist(msg, '查商品'), true);
  const rendered = msg.querySelectorAll('*').map(node => node.textContent);
  assert.ok(rendered.includes(title));
  assert.ok(rendered.includes('9007199254740993.00'));
  assert.equal(msg.querySelector('img'), null);
});

test('policy choice invokes knowledge only when the user selects it', async () => {
  const query = '退款规则是什么';
  const h = harness(() => ({ type: 'policy_choice', query, operation: 'refund_application', choices: [
    { id: 'knowledge', label: '查看退款规则' }, { id: 'refund_application', label: '申请退款' }
  ] }));
  const msg = h.addMessage();
  assert.equal(await h.commerce.assist(msg, query), true);
  assert.deepEqual(h.knowledge, []);
  await msg.querySelectorAll('button').find(button => button.textContent === '查看退款规则').listeners.click();
  assert.deepEqual(h.knowledge, [query]);
});

test('policy choices preserve the parsed order ID and display the exact refund for confirmation', async () => {
  const query = '订单 O123 申请退款，退款规则是什么';
  const h = harness(({ path, body }) => {
    if (path.endsWith('/confirmations')) {
      assert.deepEqual(body, { session_id: sessionId, operation: 'refund_application', order_id: orderId, reason: '拍错商品' });
      return { ...confirmation, status: 'pending' };
    }
    if (body.operation) {
      assert.deepEqual(body, { session_id: sessionId, operation: 'refund_application', order_id: orderId });
      return { type: 'operation', operation: 'refund_application', order_id: orderId, summary: confirmation.preview };
    }
    return { type: 'policy_choice', query: '退款规则是什么', operation: 'refund_application', order_id: orderId,
      choices: [{ id: 'refund_application', label: '申请退款' }] };
  });
  const msg = h.addMessage();
  assert.equal(await h.commerce.assist(msg, query), true);
  await msg.querySelector('button').listeners.click();
  assert.ok(msg.querySelectorAll('*').some(node => node.textContent === '125.00'));
  msg.querySelector('textarea').value = '拍错商品';
  await msg.querySelector('button').listeners.click();
  assert.ok(msg.querySelectorAll('*').some(node => node.textContent === '拍错商品'));
  assert.ok(msg.querySelectorAll('button').some(node => node.textContent === '提交退款申请'));
  assert.deepEqual(h.calls.map(call => call.path), ['/api/commerce/assist', '/api/commerce/assist', '/api/commerce/confirmations']);
});

test('product disambiguation includes an ID form if the lookup has no suggested products', async () => {
  const h = harness(({ body }) => body.query ? {
    type: 'choose', operation: 'product_detail', choices: [], message: '请输入商品编号。'
  } : {
    type: 'result', operation: 'product_detail', result: { product_id: body.product_id, title: '样品' }
  });
  const msg = h.addMessage();
  await h.commerce.assist(msg, '这个商品多少钱');
  assert.ok(msg.querySelectorAll('*').some(node => node.textContent === '请输入商品编号。'));
  const form = msg.querySelector('form');
  assert.ok(form);
  form.querySelector('input').value = 'P_123';
  await form.listeners.submit({ preventDefault() {} });
  assert.deepEqual(h.calls[1].body, { session_id: sessionId, operation: 'product_detail', product_id: 'P_123' });
  assert.ok(msg.querySelectorAll('*').some(node => node.textContent === '样品'));
});

test('entering an older order ID preserves the selected commerce operation', async () => {
  const olderOrder = 'older-order-41';
  const h = harness(({ body }) => body.query ? {
    type: 'choose', operation: 'logistics', choices: [{ id: orderId, label: '近期订单' }], message: '请选择订单，或输入订单 ID。'
  } : {
    type: 'result', operation: 'logistics', result: { order_id: body.order_id, tracking_number: 'T123',
      logistics_company: '快递', status: 'shipped', status_desc: '已发货', traces: [] }
  });
  const msg = h.addMessage();
  await h.commerce.assist(msg, '帮我查物流');
  const form = msg.querySelector('form');
  assert.ok(form);
  form.querySelector('input').value = olderOrder;
  await form.listeners.submit({ preventDefault() {} });
  assert.deepEqual(h.calls[1].body, { session_id: sessionId, operation: 'logistics', order_id: olderOrder });
  assert.ok(msg.querySelectorAll('*').some(node => node.textContent === 'T123'));
});

test('orders with the same title can be selected by their visible IDs', async () => {
  const h = harness(({ body }) => body.query ? {
    type: 'choose', operation: 'order_status', choices: [
      { id: 'A101', label: '咖啡机' }, { id: 'A102', label: '咖啡机' }
    ]
  } : {
    type: 'result', operation: 'order_status', result: { order_id: body.order_id, status: 'paid', status_desc: '已支付' }
  });
  const msg = h.addMessage();
  await h.commerce.assist(msg, '查订单状态');
  const labels = msg.querySelectorAll('button');
  assert.ok(labels.some(button => button.textContent.includes('A102')));
  const selected = labels.find(button => button.textContent.includes('A101'));
  assert.ok(selected);
  await selected.listeners.click();
  assert.deepEqual(h.calls[1].body, { session_id: sessionId, operation: 'order_status', order_id: 'A101' });
});

test('confirmation refresh only reads status; explicit retry uses the original ID and renders receipt', async () => {
  const h = harness(({ method, path }) => {
    if (!path.startsWith(confirmationPath)) return;
    return method === 'GET' ? confirmation : {
      ...confirmation, status: 'succeeded', retryable: false,
      result: { request_type: 'refund_application', request_id: 'refund-42', order_id: orderId,
        status: 'submitted', status_desc: '已受理' }
    };
  }, { stored: confirmationId, withWorkspace: true });
  await h.commerce.restore();
  assert.deepEqual(h.calls.filter(({ path }) => path.startsWith(confirmationPath)).map(({ method }) => method), ['GET']);
  const message = h.messages[0];
  const retry = message.querySelectorAll('button').find(button => button.textContent === '重试提交');
  assert.ok(retry);
  await retry.listeners.click();
  assert.deepEqual(h.calls.find(({ method }) => method === 'POST'), {
    path: confirmationPath + '/submit', method: 'POST', body: {}
  });
  assert.ok(message.querySelectorAll('*').some(node => node.textContent === 'refund-42'));
  assert.deepEqual([...h.storage], [[storageKey, JSON.stringify([confirmationId])]]);
});

test('a recent-object outage still lets users recover an unknown receipt', async () => {
  const h = harness(({ path }) => {
    if (path === '/api/commerce/objects') throw Object.assign(new Error('商城列表暂不可用'), { status: 503 });
    if (path === confirmationPath) return confirmation;
  }, { stored: confirmationId, withWorkspace: true });
  await h.commerce.restore();
  assert.match(textOf(h.workspace), /商城列表暂不可用/);
  assert.equal(h.messages.length, 1);
  assert.ok(findButton(h.messages[0], '查询提交状态'));
  assert.ok(h.calls.every(call => call.method === 'GET'));
});

test('a selected order supplies a missing target only after commerce identifies the operation', async () => {
  const h = harness(({ body }) => {
    if (!body) return;
    if (body.query === '咖啡机如何安装') return { type: 'knowledge' };
    if (body.query === '查 ORD72 的物流') return {
      type: 'result', operation: 'logistics', result: { order_id: 'ORD72', status: 'shipped', traces: [] }
    };
    if (body.query === '查 ORD72 和 ORD73 的物流') return {
      type: 'choose', operation: 'logistics', accepts_selection: false, choices: [],
      message: '请输入一个订单编号。'
    };
    if (body.query) return {
      type: 'choose', operation: 'logistics', choices: recentObjects.orders.map(order => ({
        id: order.order_id, label: order.title
      }))
    };
    return { type: 'result', operation: 'logistics', result: { order_id: body.order_id, status: 'paid', traces: [] } };
  }, { withWorkspace: true });
  await h.commerce.restore();
  assert.equal(h.workspace.hidden, false);
  const order = findButton(h.workspace, orderId);
  assert.ok(order);
  await order.listeners.click();
  assert.equal(order.attributes['aria-pressed'], 'true');
  assert.equal(await h.commerce.assist(h.addMessage(), '咖啡机如何安装'), false);
  await h.commerce.assist(h.addMessage(), '查 ORD72 的物流');
  const ambiguous = h.addMessage();
  await h.commerce.assist(ambiguous, '查 ORD72 和 ORD73 的物流');
  assert.match(textOf(ambiguous), /请输入一个订单编号/);
  const message = h.addMessage();
  await h.commerce.assist(message, '帮我查物流');
  assert.match(textOf(message), new RegExp(orderId));
  h.commerce.clearSelection();
  assert.equal(order.attributes['aria-pressed'], 'false');
  await h.commerce.assist(h.addMessage(), '帮我查物流');
  assert.deepEqual(postBodies(h), [
    { query: '咖啡机如何安装', session_id: sessionId },
    { query: '查 ORD72 的物流', session_id: sessionId },
    { query: '查 ORD72 和 ORD73 的物流', session_id: sessionId },
    { query: '帮我查物流', session_id: sessionId },
    { operation: 'logistics', order_id: orderId, session_id: sessionId },
    { query: '帮我查物流', session_id: sessionId },
  ]);
  assert.deepEqual([...h.storage], []);
});

test('object tabs show one list and send only the current verified selection to the composer', async () => {
  let session = scope;
  const h = harness(({ path }) => {
    if (path.includes('/session?')) return { authenticated: Boolean(session), account_scope: session };
  }, { withWorkspace: true });
  await h.commerce.restore();
  const groups = h.workspace.querySelectorAll('.commerce-object-group');
  const tabs = h.workspace.querySelectorAll('.commerce-object-tab');
  assert.equal(tabs.length, 2);
  assert.equal(tabs[0].attributes['aria-pressed'], 'true');
  assert.equal(tabs[1].attributes['aria-pressed'], 'false');
  assert.equal(groups[0].hidden, false);
  assert.equal(groups[1].hidden, true);

  await tabs[1].listeners.click();
  assert.equal(tabs[1].attributes['aria-pressed'], 'true');
  assert.equal(groups[0].hidden, true);
  assert.equal(groups[1].hidden, false);
  await findButton(groups[1], productId).listeners.click();
  const selected = h.selectionChanges.at(-1);
  assert.equal(selected.field, 'product_id');
  assert.equal(selected.id, productId);
  assert.equal(selected.title, '电源适配器');
  h.commerce.clearSelection();
  assert.equal(h.selectionChanges.at(-1), null);

  await findButton(groups[1], productId).listeners.click();
  session = '';
  h.listeners.blur();
  await h.listeners.focus();
  assert.equal(h.selectionChanges.at(-1), null);
  assert.equal(h.workspace.querySelectorAll('.commerce-object').length, 0);
  assert.ok(h.calls.every(call => call.method === 'GET'));
});

test('account reload replaces private objects and clears the prior selection', async () => {
  let account = '';
  let readAccount;
  const h = harness(({ path, body }) => {
    if (path.includes('/session?')) {
      if (account === 'account-b') {
        return new Promise(resolve => { readAccount = () => resolve({ authenticated: true, account_scope: account }); });
      }
      return { authenticated: Boolean(account), account_scope: account };
    }
    if (path.endsWith('/objects') && account === 'account-b') return { orders: [], products: [] };
    if (body) return { type: 'result', operation: 'product_detail', result: recentObjects.products[0] };
  }, { withWorkspace: true });
  await h.commerce.restore();
  assert.match(textOf(h.workspace), /登录/);
  assert.ok(h.calls.every(call => !call.path.endsWith('/objects')));
  account = scope;
  await findButton(h.workspace, '刷新').listeners.click();
  await findButton(h.workspace, '商品').listeners.click();
  await findButton(h.workspace, productId).listeners.click();
  const message = h.addMessage();
  const userMessage = element('div', 'msg user', '查商品 P17');
  await h.commerce.assist(message, userMessage.textContent, userMessage);
  assert.match(textOf(message), /电源适配器/);
  const beforeFocus = h.calls.length;
  await h.listeners.focus();
  assert.equal(h.calls.length, beforeFocus);
  account = 'account-b';
  h.listeners.blur();
  const refresh = h.listeners.focus();
  assert.equal(message.hidden, true);
  assert.equal(userMessage.hidden, true);
  readAccount();
  await refresh;
  assert.doesNotMatch(textOf(h.workspace), /电源适配器|伺服电机/);
  assert.match(textOf(h.workspace), /暂无订单/);
  assert.equal(h.selectionChanges.at(-1), null);
  assert.equal(message.hidden, true);
  assert.equal(userMessage.hidden, true);
});

test('the workbench signs in through commerce and clears the password and private objects on logout', async () => {
  let authenticated = false;
  const h = harness(({ path, body }) => {
    if (path === '/api/commerce/session?include_scope=true') {
      return { authenticated, account_login_enabled: true, account_scope: authenticated ? scope : undefined };
    }
    if (path === '/api/commerce/login') {
      assert.equal(body.user_id, 'fixture-user');
      assert.equal(body.password, 'fixture-secret  ');
      authenticated = true;
      return { authenticated };
    }
    if (path === '/api/commerce/logout') {
      authenticated = false;
      return { authenticated };
    }
  }, { withWorkspace: true });

  const form = h.workspace.querySelector('.commerce-login-form');
  const password = h.workspace.querySelector('.commerce-account-password');
  const logout = h.workspace.querySelector('.commerce-logout');
  await h.commerce.restore();
  assert.equal(form.hidden, false);
  assert.equal(logout.hidden, true);
  h.workspace.querySelector('.commerce-account-id').value = 'fixture-user';
  password.value = 'fixture-secret  ';
  await form.listeners.submit({ preventDefault() {} });
  assert.equal(password.value, '');
  assert.equal(form.hidden, true);
  assert.equal(logout.hidden, false);
  assert.ok(textOf(h.workspace).includes(orderId));

  await logout.listeners.click();
  assert.equal(form.hidden, false);
  assert.equal(logout.hidden, true);
  assert.ok(!textOf(h.workspace).includes(orderId));
  assert.deepEqual(h.knowledge, []);
  assert.ok(!JSON.stringify([...h.storage]).includes('fixture-secret'));
});

test('a draft reply is protected and cancellation reaches the confirmation endpoint', async () => {
  let active = { operation: 'refund_application', stage: 'missing_reason', order_id: orderId };
  const h = harness(({ path, body }) => {
    if (path === taskPath) return { active, paused: null };
    if (path === '/api/commerce/assist' && body.query === '买错了') {
      active = { ...active, stage: 'awaiting_confirmation', confirmation_id: confirmationId };
      return { type: 'confirmation', confirmation: { ...confirmation, status: 'pending' } };
    }
    if (path === confirmationPath + '/cancel') {
      active = null;
      return { ...confirmation, status: 'cancelled', retryable: false };
    }
  }, { withWorkspace: true });
  await h.commerce.restore();
  assert.match(textOf(h.workspace), /退款原因/);
  const message = h.addMessage();
  assert.equal(await h.commerce.assist(message, '买错了'), true);
  assert.ok(findButton(message, '提交退款申请'));
  await findButton(message, '取消确认').listeners.click();
  assert.deepEqual(h.calls.filter(call => call.method === 'POST'), [
    { path: '/api/commerce/assist', method: 'POST', body: { query: '买错了', session_id: sessionId, expect_task: true } },
    { path: confirmationPath + '/cancel', method: 'POST', body: {} },
  ]);
  assert.match(textOf(message), /已取消确认/);
  assert.equal(findButton(message, '提交退款申请'), undefined);
  assert.equal(findButton(h.workspace, '暂停当前任务'), undefined);
});

test('paused task controls restore the prior task when the user continues it', async () => {
  const task = { operation: 'logistics', stage: 'missing_target' };
  let active = task;
  let paused = null;
  const h = harness(({ path, body }) => {
    if (path === taskPath) return { active, paused };
    if (body?.query === '暂停当前任务') {
      active = null;
      paused = task;
      return { type: 'task_control', message: '任务已暂停。' };
    }
    if (body?.query === '继续之前的任务') {
      active = task;
      paused = null;
      return { type: 'choose', operation: 'logistics', choices: [], message: '请输入订单编号。' };
    }
  }, { withWorkspace: true });
  await h.commerce.restore();
  await findButton(h.workspace, '暂停当前任务').listeners.click();
  assert.match(textOf(h.workspace), /已暂停/);
  await findButton(h.workspace, '继续之前的任务').listeners.click();
  assert.match(textOf(h.workspace), /当前任务/);
  assert.match(textOf(h.messages.at(-1)), /请输入订单编号/);
  assert.deepEqual(postBodies(h), [
    { query: '暂停当前任务', session_id: sessionId },
    { query: '继续之前的任务', session_id: sessionId },
  ]);
});

test('a task that expires during a workspace refresh still protects the next draft reply', async () => {
  let active = { operation: 'refund_application', stage: 'missing_reason', order_id: orderId };
  const h = harness(({ path, body }) => {
    if (path === taskPath) return { active, paused: null };
    if (body) return body.expect_task
      ? { type: 'task_control', message: '当前任务已失效。' }
      : { type: 'knowledge' };
  }, { withWorkspace: true });
  await h.commerce.restore();
  active = null;
  await findButton(h.workspace, '刷新').listeners.click();
  assert.equal(await h.commerce.assist(h.addMessage(), '买错了'), true);
  assert.equal(await h.commerce.assist(h.addMessage(), '如何安装适配器'), false);
});

test('cancelling from the task panel replaces a stale pending confirmation card', async () => {
  let active = { operation: 'refund_application', stage: 'awaiting_confirmation', order_id: orderId,
    confirmation_id: confirmationId };
  const h = harness(({ path, body }) => {
    if (path === taskPath) return { active, paused: null };
    if (path === confirmationPath) return {
      ...confirmation, status: active ? 'pending' : 'cancelled', retryable: Boolean(active)
    };
    if (body?.query === '取消当前任务') {
      active = null;
      return { type: 'task_control', message: '已取消任务。' };
    }
  }, { withWorkspace: true });
  await h.commerce.restore();
  const pending = h.messages[0];
  assert.ok(findButton(pending, '提交退款申请'));
  await findButton(h.workspace, '取消当前任务').listeners.click();
  assert.match(textOf(pending), /已取消确认/);
  assert.equal(findButton(pending, '提交退款申请'), undefined);
});

test('shared JSON and SSE clients use the rendered mount path', async () => {
  const source = fs.readFileSync('app/resources/js/app.js', 'utf8');
  const calls = [];
  const sandbox = {
    window: {}, URL, location: { origin: 'http://mall.example' },
    document: { currentScript: null, documentElement: { dataset: { rootPath: '/support' } } },
    sessionStorage: { getItem: () => null },
    fetch: async url => { calls.push(url); return { ok: true, json: async () => ({}) }; },
    EventSource: class { constructor(url) { calls.push(url); } addEventListener() {} },
  };
  vm.runInNewContext(source, sandbox);
  const app = sandbox.window.App;
  app.configure({ fallbackPort: 8001 });
  await app.fetchJson(app.PATHS.query, { method: 'POST' });
  app.openStream('session-4');
  assert.deepEqual(calls, ['http://mall.example/support/api/query', 'http://mall.example/support/api/stream/session-4']);
});
