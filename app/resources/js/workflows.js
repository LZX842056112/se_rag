(function () {
  'use strict';

  const ACTIVE = new Set(['uploading', 'pending', 'processing', 'retrying', 'waiting']);
  const ALERT = new Set(['failed', 'interrupted', 'retrying']);
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
      selected: (orderedRows = rows()) => (selection && records.get(selection)) || orderedRows[0],
      select(id) { selection = id; },
      snapshot(runs) {
        records = new Map(runs.map(row => [row.id, row]));
        revisions.clear();
        runs.forEach(row => revisions.set(row.id, row.revision));
      },
      update(event, row) {
        const previous = revisions.get(row.id) ?? -1;
        if (row.revision < previous || (row.revision === previous && !row.expired)) return false;
        revisions.set(row.id, row.revision);
        if (event === 'remove') records.delete(row.id);
        else records.set(row.id, row);
        return true;
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

  App.configure({ fallbackPort: 8001 });
  const byId = id => document.getElementById(id);
  const buttons = [...byId('workflowViews').querySelectorAll('[data-view]')];
  const auth = byId('workflowAuth');
  const token = byId('workflowToken');
  const list = byId('workflowRuns');
  const cards = new Map();
  const sceneConfig = window.workflowSceneConfig || {};
  const scene = window.createWorkflowScene(paintStatus, sceneConfig);
  const shortId = id => id.replace(/^candidate:|^answer_/, '').slice(0, 8);
  const dateFormat = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const timeFormat = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
  let view, store, graph, graphKey, controller, retryTimer, renderFrame;
  let displayedAlerts = '';
  let enabled = true;

  function connection(state, message) {
    const label = byId('workflowConnection');
    label.dataset.state = state;
    label.textContent = message;
  }

  function paintStatus(element, state) {
    const [icon, label] = STATES[state] || STATES.unknown;
    if (element.dataset.state === state) return label;
    element.dataset.state = state;
    const symbol = App.create('i', '', icon);
    symbol.setAttribute('aria-hidden', 'true');
    const text = App.create('span', '', label);
    element.replaceChildren(symbol, text);
    return label;
  }

  function setText(element, value) {
    if (element.textContent !== value) element.textContent = value;
  }

  function updateMetrics(rows) {
    let active = 0, completed = 0, alerts = 0, latest = 0;
    for (const row of rows) {
      if (ACTIVE.has(row.status)) active++;
      if (row.status === 'completed') completed++;
      if (ALERT.has(row.status) || Object.values(row.nodes || {}).some(node => ALERT.has(node.status))) alerts++;
      latest = Math.max(latest, row.updated_at || 0);
    }
    setText(byId('workflowActive'), graph ? String(active) : '—');
    setText(byId('workflowCompleted'), graph ? String(completed) : '—');
    setText(byId('workflowAlerts'), graph ? String(alerts) : '—');
    byId('workflowMetrics').toggleAttribute('data-alert', alerts > 0);
    setText(byId('workflowUpdated'), latest ? timeFormat.format(latest * 1000) : '—');
  }

  function updateAlerts(selected) {
    const alerts = graph?.nodes.filter(node => ALERT.has(selected?.nodes?.[node.id]?.status)) || [];
    const alertList = alerts.map(node => [sceneConfig.nodes?.[node.id]?.label ?? node.label, selected.nodes[node.id].status]);
    const key = JSON.stringify(alertList);
    if (key === displayedAlerts) return;
    displayedAlerts = key;
    byId('workflowAttention').hidden = !alertList.length;
    byId('workflowAlertList').replaceChildren(...alertList.map(([label, state]) => {
      const item = App.create('li', '', label);
      const status = App.create('span', 'workflow-status');
      paintStatus(status, state);
      item.append(status);
      return item;
    }));
  }

  function queueRender() {
    if (!renderFrame) renderFrame = requestAnimationFrame(render);
  }

  function render() {
    renderFrame = 0;
    const rows = store.rows();
    const selected = store.selected(rows);
    const visible = new Set(rows.map(row => row.id));
    setText(byId('workflowCount'), rows.length ? `${rows.length} 条` : '');
    byId('workflowEmpty').hidden = rows.length > 0;
    for (const [id, entry] of cards) {
      if (!visible.has(id)) {
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
        button.addEventListener('click', () => { store.select(row.id); queueRender(); });
        cards.set(row.id, { button, name, status, time });
      }
      const entry = cards.get(row.id);
      const name = row.label === '问答' || row.label === '反馈' || row.label === '候选' || row.label === '调度轮次'
        ? `${row.label} ${shortId(row.id)}` : row.label;
      setText(entry.name, name);
      entry.name.title = row.label;
      entry.button.setAttribute('aria-pressed', String(selected?.id === row.id));
      paintStatus(entry.status, row.status);
      if (entry.timestamp !== row.updated_at) {
        entry.time.textContent = row.updated_at ? dateFormat.format(row.updated_at * 1000) : '';
        entry.timestamp = row.updated_at;
      }
      if (list.children[index] !== entry.button) list.insertBefore(entry.button, list.children[index] || null);
    });

    setText(byId('graphHeading'), VIEWS[view] + '流程图');
    const info = byId('workflowRunInfo');
    const status = byId('workflowRunStatus');
    info.hidden = status.hidden = !selected;
    if (selected) {
      setText(info, `${selected.id}${selected.attempt > 1 ? ` · 第 ${selected.attempt} 次尝试` : ''}`);
      paintStatus(status, selected.status);
    }
    const notice = byId('workflowNotice');
    const message = !enabled ? (view === 'evolution-cycle' ? '调度器已关闭。' : '自进化写入已关闭。')
      : DETAILS[selected?.detail] || DETAILS[selected?.error_code] || (selected?.legacy ? '部分节点没有历史状态。' : '');
    setText(notice, (selected?.id.startsWith('candidate:') ? '来源未知。' : '') + message);
    notice.hidden = !notice.textContent;
    updateMetrics(rows);
    updateAlerts(selected);
    scene.setRun(selected);
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
      const url = `${App.base}/api/workflows/${view}/events${params.size ? `?${params}` : ''}`;
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
          const key = JSON.stringify(data.graph);
          if (key !== graphKey) {
            graph = data.graph;
            graphKey = key;
            scene.setGraph(graph, view);
            byId('workflowLegend').hidden = !graph;
          }
          connection('live', '实时更新');
          queueRender();
        } else if ((event === 'run' || event === 'remove') && store.update(event, data)) queueRender();
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
    graph = graphKey = null;
    scene.setGraph(null, view);
    byId('workflowLegend').hidden = true;
    cards.clear();
    list.replaceChildren();
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === next)));
    auth.hidden = next === 'query';
    byId('workflowTokenLabel').textContent = next === 'import' ? '导入令牌' : '自进化管理令牌';
    token.value = next === 'query' ? '' : (next === 'import' ? App.getImportToken() : App.getAdminToken());
    history.replaceState(null, '', `?view=${next}`);
    queueRender();
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
  changeView(new URLSearchParams(location.search).get('view'));
})();
