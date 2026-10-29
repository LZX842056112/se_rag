const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/resources/js/chat.js', 'utf8');

function element(tag = 'div', className = '', text = '') {
  const node = { tag, className: className || '', textContent: text, children: [], dataset: {}, style: {}, disabled: false,
    listeners: {}, appendChild(child) { this.children.push(child); return child; },
    addEventListener(event, callback) { this.listeners[event] = callback; },
    querySelectorAll(selector) {
      return this.children.flatMap(child => [child, ...child.querySelectorAll(selector)])
        .filter(child => child.className.split(/\s+/).includes(selector.slice(1)));
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  };
  node.classList = { add(name) { if (!node.className.split(/\s+/).includes(name)) node.className += ' ' + name; },
    remove(name) { node.className = node.className.split(/\s+/).filter(item => item !== name).join(' '); } };
  return node;
}

function render(feedback) {
  let requests = 0;
  const functions = source.slice(source.indexOf('  function buildBotMeta('), source.indexOf('  function finalizeBotAnswer('));
  const api = vm.runInNewContext(functions + '\n({ buildBotMeta, renderCitationsAndFeedback })', {
    App: { create: element, fetchJson: async () => requests++, PATHS: { feedback: '/feedback' } },
    showToast: message => assert.fail(message),
  });
  const message = element();
  const meta = api.buildBotMeta([], null, 's', 'q', [], 'rag', 'answer-a', feedback);
  api.renderCitationsAndFeedback(message, meta);
  return { message, buttons: message.querySelectorAll('.fb-btn'), requests: () => requests };
}

test('history restores both feedback choices and prevents resubmission after refresh', async () => {
  const h = render({ thumbs: -1, resolved: false });
  assert.equal(h.buttons.length, 4);
  assert.deepEqual(h.buttons.map(button => button.disabled), [true, true, true, true]);
  assert.deepEqual(h.buttons.filter(button => button.className.includes('active')).map(button => button.textContent), ['👎 没帮助', '仍未解决']);
  await h.buttons[1].listeners.click();
  assert.equal(h.requests(), 0);
});

test('a restored helpfulness choice leaves the resolution controls available', () => {
  const h = render({ thumbs: 1, resolved: null });
  assert.deepEqual(h.buttons.map(button => button.disabled), [true, true, false, false]);
  assert.equal(h.buttons[0].className.includes('active'), true);
});

test('an unanswered feedback form keeps all choices available', () => {
  const h = render(null);
  assert.deepEqual(h.buttons.map(button => button.disabled), [false, false, false, false]);
});

test('history loader forwards saved feedback when rebuilding the assistant message', async () => {
  const start = source.indexOf('  async function loadHistory(');
  const end = source.indexOf('  function submitQuery(', start);
  const calls = [];
  const feedback = { thumbs: 1, resolved: true };
  const fn = vm.runInNewContext('(' + source.slice(start, end).trim() + ')', {
    App: { PATHS: { history: () => '/history' }, fetchJson: async () => ({ items: [
      { role: 'assistant', id: 'a', text: 'answer', feedback }, { role: 'user', text: 'question' }
    ] }) },
    sessionId: 's', chatEl: { querySelectorAll: () => [] },
    addUserMsgWithTime() {}, addBotMsgWithTime: (...args) => calls.push(args), scrollToBottom() {},
    showToast: message => assert.fail(message), console,
  });
  await fn();
  assert.deepEqual(calls[0][10], feedback);
});
