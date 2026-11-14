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

test('the perspective camera fits the board and tall nodes at desktop and phone sizes', () => {
  const { createProjection } = require('../../app/resources/js/workflow-scene.js');
  const world = { width: 640, height: 920, bottom: -28, top: 96 };
  for (const viewport of [{ width: 292, height: 430 }, { width: 1050, height: 550 }]) {
    const { project } = createProjection(world, viewport, { azimuth: -28, pitch: 46, zoom: 1, x: 0, y: 0 }, 24);
    for (const x of [0, world.width]) {
      for (const y of [0, world.height]) {
        for (const z of [world.bottom, world.top]) {
          const point = project(x, y, z);
          assert.ok(point.x >= 23.999 && point.x <= viewport.width - 23.999);
          assert.ok(point.y >= 23.999 && point.y <= viewport.height - 23.999);
        }
      }
    }
  }
  const { project } = createProjection(world, { width: 1200, height: 640 },
    { azimuth: 0, pitch: 48, zoom: 1, x: 0, y: 0 }, 32);
  const widthAt = y => project(500, y).x - project(140, y).x;
  assert.ok(widthAt(760) > widthAt(160), 'equal modules appear larger near the camera');
  assert.ok(project(320, 400, 60).y < project(320, 400, 0).y);
});

test('configured rows keep every live node visible even when the layout is stale', () => {
  const { layoutNodes } = require('../../app/resources/js/workflow-scene.js');
  const layout = layoutNodes(['start', 'one', 'two', 'new'], [
    ['stale'], [null, 'start'], ['two', 'one', 'start']
  ]);
  assert.equal(layout.columns, 3);
  assert.deepEqual([...layout.positions], [
    ['start', [1, 2]], ['two', [2, 1]], ['one', [2, 2]], ['new', [3, 2]]
  ]);
});

test('a moving beam follows the complete route at a constant speed through a turn', () => {
  const { measurePath, pointAt } = require('../../app/resources/js/workflow-scene.js');
  const route = measurePath([[0, 0], [0, 0], [10, 0], [10, 20], [0, 20]]);
  assert.equal(route.length, 40);
  assert.deepEqual(pointAt(route, -1), [0, 0]);
  assert.deepEqual(pointAt(route, 15), [10, 5]);
  assert.deepEqual(pointAt(route, 35), [5, 20]);
  assert.deepEqual(pointAt(route, 50), [0, 20]);
});
