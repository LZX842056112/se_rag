const assert = require('node:assert/strict');
const test = require('node:test');

test('workflow SSE parser handles split UTF-8, CRLF, heartbeats and multiple events', async () => {
  const { readEvents } = require('../../app/resources/js/workflows.js');
  const payload = new TextEncoder().encode(': heartbeat\r\n\r\nevent: run\r\ndata: {"label":"问答"}\r\n\r\nevent: remove\ndata: {"id":"one"}\n\n');
  const body = new ReadableStream({ start(controller) {
    for (let index = 0; index < payload.length; index += 2) controller.enqueue(payload.slice(index, index + 2));
    controller.close();
  } });
  const events = [];
  await readEvents(body, (...event) => events.push(event));
  assert.deepEqual(events, [['run', { label: '问答' }], ['remove', { id: 'one' }]]);
});

test('live updates preserve manual selection and ignore older run revisions', () => {
  const { createRunList } = require('../../app/resources/js/workflows.js');
  const store = createRunList();
  const row = (id, status, revision) => ({ id, status, revision, updated_at: revision });
  store.snapshot([row('old', 'completed', 1), row('active', 'processing', 3)]);
  assert.equal(store.selected().id, 'active');
  store.select('old');
  store.update('run', row('new', 'processing', 4));
  store.update('run', row('active', 'failed', 2));
  assert.equal(store.selected().id, 'old');
  assert.equal(store.rows().find(item => item.id === 'active').status, 'processing');
  store.snapshot([row('old', 'completed', 1), row('new', 'completed', 5)]);
  assert.equal(store.selected().id, 'old');
  store.update('remove', { id: 'old', revision: 6 });
  store.update('run', row('old', 'processing', 3));
  assert.equal(store.selected().id, 'new');
});
