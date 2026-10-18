const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/resources/js/chat.js', 'utf8');
const start = source.indexOf('async function restoreSession()');
const end = source.indexOf('\n  // 不能直接绑 onSend', start);
const code = '(' + source.slice(start, end).trim() + ')';
function harness(history, status) {
  const result = { attached: 0, users: [], skeletons: 0 };
  const sandbox = {
    fetchQueryStatus: async () => status,
    loadHistory: async () => history,
    ACTIVE_TASK_STATUS: { pending: true, processing: true },
    addUserMsg: text => result.users.push(text),
    addBotMsgSkeleton: () => { result.skeletons++; return {}; },
    renderProgress: () => {},
    sendBtn: { disabled: false },
    attachStream: () => result.attached++,
    sessionId: 's'
  };
  result.run = vm.runInNewContext(code, sandbox);
  result.sandbox = sandbox;
  return result;
}
for (const lastUserText of ['旧问题', '重复问题']) {
  test('refresh restores new turn after old assistant: ' + lastUserText, async () => {
    const h = harness({ lastRole: 'assistant', lastUserText, lastTurnId: 'old', lastUserTurnId: 'old' },
      { status: 'processing', query: '重复问题', turn_id: 'new', done_list: [], running_list: [] });
    await h.run();
    assert.equal(h.attached, 1);
    assert.deepEqual(h.users, ['重复问题']);
    assert.equal(h.sandbox.sendBtn.disabled, true);
  });
}
test('first question restores missing user', async () => {
  const h = harness({ lastRole: '', lastUserText: '', lastTurnId: '', lastUserTurnId: '' },
    { status: 'processing', query: '你好', turn_id: 'new' });
  await h.run();
  assert.equal(h.attached, 1);
  assert.deepEqual(h.users, ['你好']);
});
test('saved answer of same turn is not duplicated', async () => {
  const h = harness({ lastRole: 'assistant', lastUserText: '你好', lastTurnId: 'new', lastUserTurnId: 'new' },
    { status: 'processing', query: '你好', turn_id: 'new' });
  await h.run();
  assert.equal(h.attached, 0);
  assert.equal(h.skeletons, 0);
});
test('saved current user is not duplicated', async () => {
  const h = harness({ lastRole: 'user', lastUserText: '你好', lastTurnId: 'new', lastUserTurnId: 'new' },
    { status: 'processing', query: '你好', turn_id: 'new' });
  await h.run();
  assert.equal(h.attached, 1);
  assert.deepEqual(h.users, []);
});
