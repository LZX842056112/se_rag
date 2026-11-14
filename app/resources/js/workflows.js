(function () {
  'use strict';

  const ACTIVE = new Set(['uploading', 'pending', 'processing', 'retrying', 'waiting']);
  const VIEWS = {
    import: '文件导入', query: '问答', 'evolution-cycle': '调度轮次', 'evolution-candidate': '候选生命周期'
  };
  const STATES = {
    uploading: ['↑', '保存中'], pending: ['◷', '排队中'], processing: ['●', '运行中'], waiting: ['◷', '等待中'],
    running: ['●', '运行中'], completed: ['✓', '已完成'], idle: ['○', '未执行'], skipped: ['–', '已跳过'],
    retrying: ['↻', '等待重试'], failed: ['!', '失败'], unknown: ['?', '状态未知'], interrupted: ['!', '已中断'],
    closed: ['✓', '已关闭'], rejected: ['×', '已驳回'], withdrawn: ['↓', '已下架']
  };
  const DETAILS = {
    need_info: '等待补充资料。', awaiting_approval: '等待人工审批。', weak: '反馈未达到缺口阈值。',
    ignored: '已忽略该反馈。', resolved: '问题已解决。', outside_window: '反馈已超出观察窗口。',
    import_busy: '等待导入名额超时。', scheduler_stage_error: '一个或多个步骤未完成。'
  };
  // Coordinates give each branch and cycle room. The server supplies the topology.
  const POSITIONS = {
    import: [['__start__'], ['upload_file'], ['node_entry'], [null, null, 'node_pdf_to_md'],
      ['node_md_img'], ['node_document_split'], ['node_item_name_recognition'], ['node_bge_embedding'],
      ['node_import_milvus'], ['__end__']],
    query: [['__start__'], ['node_intent_route'], ['node_item_name_confirm', null, 'node_chitchat_answer'],
      ['node_agentic', 'node_search_embedding', 'node_search_embedding_hyde'],
      ['agentic:__start__', 'node_web_search_mcp'], ['agentic:bootstrap', 'node_rrf'],
      ['agentic:assess', 'node_rerank'], ['agentic:select', 'node_parent_expand'],
      ['agentic:execute', 'node_answer_output'], ['agentic:draft'], ['agentic:verify'],
      ['agentic:__end__'], ['__end__']],
    'evolution-candidate': [['feedback'], ['gap'], ['candidate', null, 'strategy'], ['supplement'],
      ['approval', null, 'reject'], ['publish'], ['withdraw'], ['__end__']]
  };

  function createRunList() {
    let records = new Map();
    const revisions = new Map();
    let selection = '';
    function rows() {
      const sorted = [...records.values()].sort((a, b) => b.updated_at - a.updated_at || b.revision - a.revision);
      const active = sorted.filter(row => ACTIVE.has(row.status));
      const recent = sorted.filter(row => !ACTIVE.has(row.status)).slice(0, 50);
      if (selection && records.has(selection) && !active.concat(recent).some(row => row.id === selection)) {
        recent[recent.length - 1] = records.get(selection);
      }
      return active.concat(recent);
    }
    return {
      rows,
      selected: () => (selection && records.get(selection)) || rows()[0],
      select(id) { selection = id; },
      snapshot(runs) {
        records = new Map(runs.map(row => [row.id, row]));
        revisions.clear();
        runs.forEach(row => revisions.set(row.id, row.revision));
      },
      update(event, row) {
        const previous = revisions.get(row.id) ?? -1;
        if (row.revision < previous || (row.revision === previous && !row.expired)) return;
        revisions.set(row.id, row.revision);
        if (event === 'remove') records.delete(row.id);
        else records.set(row.id, row);
      }
    };
  }

  async function readEvents(body, dispatch) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let event = '';
    let data = [];
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        let newline = buffer.indexOf('\n');
        while (newline !== -1) {
          const line = buffer.slice(0, newline).replace(/\r$/, '');
          buffer = buffer.slice(newline + 1);
          if (!line) {
            if (event && data.length) dispatch(event, JSON.parse(data.join('\n')));
            event = '';
            data = [];
          } else if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
          newline = buffer.indexOf('\n');
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { createRunList, readEvents };
    return;
  }

  const byId = id => document.getElementById(id);
  const buttons = [...byId('workflowViews').querySelectorAll('[data-view]')];
  const auth = byId('workflowAuth');
  const token = byId('workflowToken');
  const diagram = byId('workflowDiagram');
  const canvas = byId('workflowEdges');
  const nodeContainer = byId('workflowNodes');
  const edgeList = byId('workflowEdgeList');
  const list = byId('workflowRuns');
  const cards = new Map();
  const elements = new Map();
  const shortId = id => id.replace(/^candidate:|^answer_/, '').slice(0, 8);
  const dateFormat = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  let view, store, graph, controller, retryTimer, drawFrame;
  let enabled = true;

  function connection(state, message) {
    const label = byId('workflowConnection');
    label.dataset.state = state;
    label.textContent = message;
  }

  function paintStatus(element, state) {
    const [icon, label] = STATES[state] || STATES.unknown;
    element.dataset.state = state;
    const symbol = App.create('i', '', icon);
    symbol.setAttribute('aria-hidden', 'true');
    element.replaceChildren(symbol, document.createTextNode(label));
  }

  function buildGraph(next) {
    if (graph === next) return;
    graph = next;
    elements.clear();
    nodeContainer.replaceChildren();
    edgeList.replaceChildren();
    diagram.hidden = !graph;
    byId('workflowLegend').hidden = !graph;
    if (!graph) return;

    const vertical = view === 'evolution-cycle';
    const layout = POSITIONS[view] || graph.nodes.map(node => [node.id]);
    const positions = new Map();
    layout.forEach((row, index) => row.forEach((id, col) => {
      if (id) positions.set(id, [index + 1, row.length === 1 && !vertical && id !== '__end__' && id !== '__start__' && view === 'query' ? 1 : (row.length === 1 && !vertical ? 2 : col + 1)]);
    }));
    nodeContainer.style.setProperty('--columns', vertical ? '1' : '3');
    nodeContainer.style.setProperty('--mobile-columns', vertical ? '1' : '3');
    const labels = new Map(graph.nodes.map(node => [node.id, node.label]));
    const ordered = [...positions.keys()].filter(id => labels.has(id));
    const extra = graph.nodes.map(node => node.id).filter(id => !positions.has(id));
    ordered.concat(extra).forEach((id, index) => {
      const [row, col] = positions.get(id) || [layout.length + index - ordered.length + 1, 1];
      const element = App.create('div', 'workflow-node');
      element.dataset.node = id;
      element.style.setProperty('--node-row', row);
      element.style.setProperty('--node-column', col);
      element.style.setProperty('--mobile-row', row);
      element.style.setProperty('--mobile-column', col);
      const status = App.create('span', 'workflow-status');
      const visits = App.create('span', 'workflow-visits');
      element.append(App.create('strong', '', labels.get(id)), status, visits);
      nodeContainer.append(element);
      elements.set(id, { element, status, visits });
    });
    graph.edges.forEach(edge => {
      edgeList.append(App.create('li', '', `${labels.get(edge.source)} → ${labels.get(edge.target)}`));
    });
  }

  function drawEdges() {
    if (!graph || diagram.hidden) return;
    const bounds = diagram.getBoundingClientRect();
    const width = diagram.clientWidth;
    const height = diagram.clientHeight;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(ratio, ratio);
    const pad = width < 460 ? 5 : 9;
    const rects = new Map([...elements].map(([id, { element }]) => {
      const rect = element.getBoundingClientRect();
      return [id, { x: rect.left - bounds.left - 1, y: rect.top - bounds.top - 1, w: rect.width, h: rect.height }];
    }));

    graph.edges.forEach(edge => {
      const a = rects.get(edge.source);
      const b = rects.get(edge.target);
      if (!a || !b) return;
      const state = nodeId => elements.get(nodeId).element.dataset.state;
      const active = ['running', 'completed'].includes(state(edge.source)) && state(edge.target) === 'running';
      ctx.strokeStyle = active ? '#315bdb' : '#8391a7';
      ctx.fillStyle = ctx.strokeStyle;
      ctx.lineWidth = active ? 2 : 1.25;
      ctx.setLineDash(edge.conditional && !active ? [4, 3] : []);
      let points;
      if (edge.source === edge.target) {
        const side = a.x + a.w < width - 15 ? a.x + a.w + pad : a.x - pad;
        const start = side > a.x ? a.x + a.w : a.x;
        points = [[start, a.y + a.h * .70], [side, a.y + a.h * .70], [side, a.y + a.h * .30], [start, a.y + a.h * .30]];
      } else if (b.y < a.y) {
        const side = Math.min(a.x, b.x) - pad;
        points = [[a.x, a.y + a.h * .66], [side, a.y + a.h * .66], [side, b.y + b.h * .34], [b.x, b.y + b.h * .34]];
      } else if (Math.abs(a.y - b.y) < 5) {
        const toRight = b.x > a.x;
        const startX = toRight ? a.x + a.w : a.x;
        const y = Math.max(a.y + a.h, b.y + b.h) + pad;
        const endMid = b.x + b.w / 2;
        points = [[startX, a.y + a.h / 2], [startX + (toRight ? pad : -pad), a.y + a.h / 2],
          [startX + (toRight ? pad : -pad), y], [endMid, y], [endMid, b.y + b.h]];
      } else {
        const start = [a.x + a.w / 2, a.y + a.h];
        const end = [b.x + b.w / 2, b.y];
        if (b.y - a.y - a.h > 48) {
          const goLeft = a.x + a.w / 2 < width / 2;
          const side = goLeft ? a.x - pad : a.x + a.w + pad;
          points = [start, [start[0], start[1] + pad], [side, start[1] + pad], [side, end[1] - pad], [end[0], end[1] - pad], end];
        } else {
          const middle = (start[1] + end[1]) / 2;
          points = [start, [start[0], middle], [end[0], middle], end];
        }
      }
      ctx.beginPath();
      points.forEach(([x, y], index) => index ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
      ctx.stroke();
      const [x, y] = points.at(-1);
      const [px, py] = points.at(-2);
      const angle = Math.atan2(y - py, x - px);
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x - 6 * Math.cos(angle - .52), y - 6 * Math.sin(angle - .52));
      ctx.lineTo(x - 6 * Math.cos(angle + .52), y - 6 * Math.sin(angle + .52));
      ctx.fill();
    });
  }

  function redraw() {
    cancelAnimationFrame(drawFrame);
    drawFrame = requestAnimationFrame(drawEdges);
  }

  function render() {
    const rows = store.rows();
    const selected = store.selected();
    byId('workflowCount').textContent = rows.length ? `${rows.length} 条` : '';
    byId('workflowEmpty').hidden = rows.length > 0;
    for (const [id, entry] of cards) {
      if (!rows.some(row => row.id === id)) {
        entry.button.remove();
        cards.delete(id);
      }
    }
    rows.forEach((row, index) => {
      if (!cards.has(row.id)) {
        const button = App.create('button', 'workflow-record');
        button.type = 'button';
        const name = App.create('strong');
        const status = App.create('span', 'workflow-status');
        const time = App.create('time');
        button.append(name, status, time);
        button.addEventListener('click', () => { store.select(row.id); render(); });
        cards.set(row.id, { button, name, status, time });
      }
      const entry = cards.get(row.id);
      const name = row.label === '问答' || row.label === '反馈' || row.label === '候选' || row.label === '调度轮次'
        ? `${row.label} ${shortId(row.id)}` : row.label;
      entry.name.textContent = name;
      entry.name.title = row.label;
      entry.button.setAttribute('aria-pressed', String(selected?.id === row.id));
      paintStatus(entry.status, row.status);
      entry.time.textContent = row.updated_at ? dateFormat.format(row.updated_at * 1000) : '';
      if (list.children[index] !== entry.button) list.insertBefore(entry.button, list.children[index] || null);
    });

    byId('graphHeading').textContent = view ? VIEWS[view] : '流程图';
    const info = byId('workflowRunInfo');
    const status = byId('workflowRunStatus');
    info.hidden = status.hidden = !selected;
    if (selected) {
      info.textContent = `${selected.id}${selected.attempt > 1 ? ` · 第 ${selected.attempt} 次尝试` : ''}`;
      paintStatus(status, selected.status);
    }
    const notice = byId('workflowNotice');
    const message = !enabled ? (view === 'evolution-cycle' ? '调度器已关闭。' : '自进化写入已关闭。')
      : DETAILS[selected?.detail] || DETAILS[selected?.error_code] || (selected?.legacy ? '部分节点没有历史状态。' : '');
    notice.textContent = (selected?.id.startsWith('candidate:') ? '来源未知。' : '') + message;
    notice.hidden = !notice.textContent;
    elements.forEach(({ element, status: label, visits }, id) => {
      const node = selected?.nodes?.[id];
      const state = node?.status || (selected?.legacy ? 'unknown' : 'idle');
      if (element.dataset.state !== state) {
        element.dataset.state = state;
        paintStatus(label, state);
      }
      visits.hidden = !(node?.visits > 1);
      visits.textContent = visits.hidden ? '' : `×${node.visits}`;
      visits.setAttribute('aria-label', `执行 ${node?.visits || 0} 次`);
    });
    redraw();
  }

  async function connect() {
    clearTimeout(retryTimer);
    if (controller) controller.abort();
    const connectionView = view;
    const active = new AbortController();
    controller = active;
    const params = new URLSearchParams();
    const headers = { Accept: 'text/event-stream' };
    if (view === 'query') {
      const session = localStorage.getItem('kb_session_id');
      if (!session) {
        connection('idle', '打开对话页并开始问答');
        return;
      }
      params.set('session_id', session);
    } else {
      const value = view === 'import' ? App.getImportToken() : App.getAdminToken();
      if (!value) {
        connection('idle', '请输入管理令牌');
        return;
      }
      headers['X-Internal-Token'] = value;
    }
    connection('idle', '正在连接');
    try {
      const url = `/api/workflows/${view}/events${params.size ? `?${params}` : ''}`;
      const response = await fetch(url, { headers, signal: active.signal, credentials: 'same-origin' });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        if (active.signal.aborted) return;
        connection('error', response.status === 401 ? '令牌无效。请重新输入。' : error.message || '无法连接');
        if (response.status < 500 || ['admin_token_missing', 'import_admin_token_missing', 'workflow_ownership_required'].includes(error.code)) return;
        throw new Error(error.message || '无法连接');
      }
      await readEvents(response.body, (event, data) => {
        if (active.signal.aborted) return;
        if (event === 'snapshot') {
          enabled = data.enabled;
          store.snapshot(data.runs);
          // Keep node elements in place during state snapshots.
          if (!graph || JSON.stringify(graph) !== JSON.stringify(data.graph)) buildGraph(data.graph);
          connection('live', '实时更新');
        } else if (event === 'run' || event === 'remove') {
          store.update(event, data);
        }
        render();
      });
      throw new Error('连接已断开');
    } catch (_) {
      if (active.signal.aborted || connectionView !== view) return;
      connection('error', '连接中断。正在重连。');
      retryTimer = setTimeout(connect, 1500);
    }
  }

  function changeView(next) {
    if (!Object.hasOwn(VIEWS, next)) next = 'query';
    view = next;
    store = createRunList();
    enabled = true;
    buildGraph(null);
    cards.clear();
    list.replaceChildren();
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === next)));
    auth.hidden = next === 'query';
    byId('workflowTokenLabel').textContent = next === 'import' ? '导入令牌' : '自进化管理令牌';
    token.value = next === 'query' ? '' : (next === 'import' ? App.getImportToken() : App.getAdminToken());
    history.replaceState(null, '', `?view=${next}`);
    render();
    connect();
  }

  buttons.forEach(button => button.addEventListener('click', () => changeView(button.dataset.view)));
  auth.addEventListener('submit', event => {
    event.preventDefault();
    if (view === 'import') App.setImportToken(token.value);
    else App.setAdminToken(token.value);
    connect();
  });
  window.addEventListener('storage', event => {
    if (view === 'query' && event.key === 'kb_session_id') changeView(view);
  });
  window.addEventListener('online', () => connect());
  window.addEventListener('pagehide', () => { controller?.abort(); clearTimeout(retryTimer); });
  window.addEventListener('pageshow', event => { if (event.persisted) connect(); });
  window.addEventListener('resize', redraw);
  new ResizeObserver(redraw).observe(nodeContainer);
  changeView(new URLSearchParams(location.search).get('view'));
})();
