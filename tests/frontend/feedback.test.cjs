const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/resources/js/chat.js', 'utf8');
const start = source.indexOf('function applyFeedbackState(');
const end = source.indexOf('\n  function renderCitationsAndFeedback(', start);

test('helpfulness feedback leaves resolution available and each group submits once', async () => {
  const requests = [];
  const buttons = ['thumbs', 'thumbs', 'resolved', 'resolved'].map(group => ({
    dataset: { feedbackGroup: group }, disabled: false, classList: { add() {} }
  }));
  const bar = { querySelectorAll: () => buttons, querySelector: () => ({}), appendChild() {} };
  const message = { querySelector: () => bar };
  const fn = vm.runInNewContext(source.slice(start, end) + '\nsubmitFeedback', {
    App: { PATHS: { feedback: '/feedback' }, fetchJson: async (url, options) => requests.push(JSON.parse(options.body)) },
    showToast: message => assert.fail(message)
  });
  const meta = { sessionId: 's', answerId: 'a', citations: [] };
  await fn(message, buttons[0], meta, 1);
  assert.equal(buttons[1].disabled, true);
  assert.equal(buttons[2].disabled, false);
  await fn(message, buttons[2], meta, null, true);
  assert.equal(requests.length, 2);
  assert.equal(requests[1].resolved, true);
  await fn(message, buttons[2], meta, null, true);
  assert.equal(requests.length, 2);
});
