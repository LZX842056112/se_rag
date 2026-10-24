const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element(tag) {
  let text = '';
  return { tag, children: [], style: {}, listeners: {},
    get textContent() { return text; },
    set textContent(value) { text = String(value); this.children = []; },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(event, fn) { this.listeners[event] = fn; }
  };
}
function page() {
  const window = {};
  vm.runInNewContext(fs.readFileSync('app/resources/js/app.js', 'utf8'), {
    window, document: { createElement: element }, URL,
    sessionStorage: { getItem() { return ''; } }
  });
  return { app: window.App, container: element('div') };
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }
function imageSources(container) { return descendants(container).filter(node => node.tag === 'img').map(node => node.src); }
const PANEL = 'https://images.local/panel.jpg';
const OK = 'https://images.local/ok.png';

test('an empty trailing image section is hidden without hiding the answer', () => {
  const { app, container } = page();
  app.renderAnswerWithImages(container, 'HL3040不支持无线网络。\n\n【图片】', []);
  assert.equal(container.children[0].textContent, 'HL3040不支持无线网络。');
  assert.deepEqual(imageSources(container), []);
});

test('text only history does not display the old automatically attached image gallery', () => {
  const { app, container } = page();
  app.renderAnswerWithImages(container, 'HAK 180 烫金机没有蓝牙。', [PANEL, OK]);
  assert.deepEqual(imageSources(container), []);
  assert.equal(container.children[0].textContent, 'HAK 180 烫金机没有蓝牙。');
});

test('streamed references wait for the final approved image list', () => {
  const { app, container } = page();
  app.renderAnswerWithImages(container, `按图操作。\n【图片】\n${PANEL}`, []);
  assert.deepEqual(imageSources(container), []);
  app.renderAnswerWithImages(container, `按图操作。\n【图片】\n${PANEL}`, [PANEL, OK]);
  assert.deepEqual(imageSources(container), [PANEL]);
});

test('markdown image renders once only when selected by the answer and the server', () => {
  const { app, container } = page();
  app.renderAnswerWithImages(container, `按图设置。\n![面板](${PANEL})\n![面板](${PANEL})\n![无关](${OK})`, [PANEL]);
  assert.deepEqual(imageSources(container), [PANEL]);
  assert.ok(!container.children[0].textContent.includes('!['));
  assert.ok(!container.children[0].textContent.includes(OK));
});

test('a loose image link in prose never becomes an image', () => {
  const { app, container } = page();
  app.renderAnswerWithImages(container, `文件地址：${PANEL}`, [PANEL]);
  assert.deepEqual(imageSources(container), []);
});

for (const answer of ['1. 点击【图片】菜单。\n2. 选择文件并确认。', '打开菜单：\n【图片】\n然后选择文件并确认。']) {
  test('image menu label does not truncate the remaining instructions: ' + answer, () => {
    const { app, container } = page();
    app.renderAnswerWithImages(container, answer, []);
    assert.equal(container.children[0].textContent, answer);
    assert.deepEqual(imageSources(container), []);
  });
}

test('refresh replaces the old image gallery and keeps the selected image failure link', () => {
  const { app, container } = page();
  app.renderAnswerWithImages(container, `设置。\n【图片】\n${PANEL}`, [PANEL]);
  const image = descendants(container).find(node => node.tag === 'img');
  const link = descendants(container).find(node => node.tag === 'a');
  image.listeners.error();
  assert.equal(link.style.display, 'inline');
  app.renderAnswerWithImages(container, '默认中间值。', [PANEL]);
  assert.deepEqual(imageSources(container), []);
});

test('tiny step badges are hidden even when the source caption describes an operation', () => {
  const { app, container } = page();
  app.renderAnswerWithImages(container, `安装说明。\n【图片】\n${PANEL}`, [PANEL]);
  const image = descendants(container).find(node => node.tag === 'img');
  const link = descendants(container).find(node => node.tag === 'a');
  image.naturalWidth = 46;
  image.naturalHeight = 42;
  image.listeners.load();
  assert.equal(image.style.display, 'none');
  assert.equal(link.style.display, 'none');
});
