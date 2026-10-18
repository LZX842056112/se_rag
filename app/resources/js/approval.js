/*
 * 自进化候选审批后台逻辑（公共能力见 /static/app.js）。
 */
(function () {
  'use strict';

  App.configure({ fallbackPort: 8001 });

  const esc = App.escapeHtml;
  const toast = App.toast;
  const fmtTs = App.formatDateTime;

  const listEl = document.getElementById('list');
  const countEl = document.getElementById('count');
  const filterEl = document.getElementById('statusFilter');
  const tokenEl = document.getElementById('adminToken');
  const statusEl = document.getElementById('evoStatus');

  const ST_LABEL = {
    draft: '待审批',
    candidate: '强缺口',
    pending: '弱缺口',
    active: '已入库',
    rejected: '已驳回',
    hold: '观察中',
    removed: '已下架',
    need_info: '待人工补充'
  };

  // 审批写操作令牌：仅当前标签页会话有效（sessionStorage），输入时即时持久化
  if (tokenEl) {
    tokenEl.value = App.getAdminToken();
    tokenEl.addEventListener('input', () => App.setAdminToken(tokenEl.value.trim()));
    tokenEl.addEventListener('change', () => { reload(); loadStatus(); });
  }

  async function loadIssues() {
    const list = document.getElementById('issueList');
    if (!list) return;
    try {
      const data = await App.fetchJson('/api/evolution/issues');
      list.textContent = '';
      (data.items || []).forEach(item => {
        const row = App.create('div', 'item');
        row.appendChild(App.create('p', null, item.query));
        const types = { missing_material: '缺资料', retrieval_failure: '检索失败', answer_error: '回答错误', complaint: '业务投诉' };
        row.appendChild(App.create('p', 'greet', (types[item.issue_type] || item.issue_type) + ' · 创建 ' + fmtTs(item.created_at) + (item.completed_at ? ' · 完成 ' + fmtTs(item.completed_at) : '')));
        const owner = document.createElement('input');
        owner.placeholder = '负责人'; owner.maxLength = 128; owner.value = item.owner || '';
        const status = document.createElement('select');
        [['open', '待处理'], ['in_progress', '处理中'], ['resolved', '已解决']].forEach(([value, label]) => {
          const opt = document.createElement('option'); opt.value = value; opt.textContent = label; status.appendChild(opt);
        });
        status.value = item.status;
        const resolution = document.createElement('textarea');
        resolution.placeholder = '处理结果'; resolution.maxLength = 2000; resolution.value = item.resolution || '';
        const save = App.create('button', 'btn', '保存处理记录');
        save.addEventListener('click', async () => {
          save.disabled = true;
          try {
            await App.fetchJson('/api/evolution/issues/' + encodeURIComponent(item.id), {
              method: 'POST', body: JSON.stringify({ owner: owner.value.trim(), status: status.value, resolution: resolution.value.trim() })
            });
            await loadIssues(); await loadTrialMetrics();
          } catch (e) { toast(e.message); save.disabled = false; }
        });
        [owner, status, resolution, save].forEach(el => row.appendChild(el));
        list.appendChild(row);
      });
      if (!(data.items || []).length) list.textContent = '当前没有待办。';
    } catch (e) { list.textContent = e.message; }
  }

  async function loadTrialMetrics() {
    const el = document.getElementById('trialMetrics');
    if (!el) return;
    try {
      const data = await App.fetchJson('/api/evolution/trial-metrics');
      const pct = value => value == null ? '未评估' : (value * 100).toFixed(1) + '%';
      el.textContent = '近24小时知识提问 ' + data.rag_queries +
        ' · 明确解决率 ' + pct(data.resolution_rate) + ' · 解决反馈覆盖率 ' + pct(data.resolution_feedback_coverage) +
        ' · 重复提问率 ' + pct(data.repeat_query_rate) + ' · 转交率 ' + pct(data.handoff_rate) +
        ' · 知识补充平均时长 ' + (data.knowledge_completion_mean_hours == null ? '未评估' : data.knowledge_completion_mean_hours.toFixed(1) + '小时');
      Object.entries(data.routes || {}).forEach(([route, metric]) => {
        const text = route + '：' + metric.count + '轮，P95 ' + Math.round(metric.duration_p95_ms) + '毫秒，平均模型调用 ' + metric.mean_llm_calls.toFixed(1) +
          '次，估算费用 ' + (metric.cost_usd == null ? '未配置或未取得用量' : '$' + metric.cost_usd.toFixed(4));
        el.appendChild(App.create('p', null, text));
      });
    } catch (e) { el.textContent = e.message; }
  }

  function shortId(id) { return String(id || '').slice(0, 8); }

  function sumCounts(counts) {
    return Object.keys(counts || {}).reduce((total, key) => total + (counts[key] || 0), 0);
  }

  function shortClock(ts) {
    return ts ? App.formatDateTime(ts) : '未执行';
  }

  /** 闭环状态：把「扫描游标 / 缺口与候选计数」显式展示，避免只能翻日志判断是否卡住 */
  async function loadStatus() {
    loadIssues();
    loadTrialMetrics();
    if (!statusEl) return;
    try {
      const data = await App.fetchJson(App.PATHS.evolutionStatus);
      if (!data.enabled) { statusEl.textContent = '闭环状态：自进化未开启'; return; }
      const gaps = sumCounts(data.gaps);
      const candidates = sumCounts(data.candidates);
      const scheduler = data.scheduler || {};
      statusEl.textContent = '闭环：缺口 ' + gaps + ' · 候选 ' + candidates +
        ' · 近一次自评 ' + shortClock(scheduler.last_metric_ts);
      statusEl.title = '缺口：' + JSON.stringify(data.gaps || {}) +
        '\n候选：' + JSON.stringify(data.candidates || {}) +
        '\n反馈事件：' + (data.feedback_events || 0) +
        '\n最近回测：' + shortClock(scheduler.last_backtest_ts) +
        '\n调度间隔（分钟）：' + (scheduler.interval_minutes || 0);
    } catch (e) {
      statusEl.textContent = '闭环状态：不可用';
    }
  }

  function chipsHtml(item) {
    const parts = [];
    if (Array.isArray(item.item_names) && item.item_names.length) {
      item.item_names.forEach(n => parts.push('<span class="chip">主体:' + esc(n) + '</span>'));
    } else {
      parts.push('<span class="chip">无主体</span>');
    }
    (Array.isArray(item.source_refs) ? item.source_refs : [])
      .slice(0, 3)
      .forEach(ref => parts.push('<span class="chip">ref:' + esc(String(ref).slice(0, 40)) + '</span>'));
    return parts.join('');
  }

  function beginEdit(item, card) {
    if (card.querySelector('.edit-box')) return; // 防重入：编辑框已存在则忽略
    const box = App.create('div', 'edit-box');
    box.innerHTML =
      '<label>问题（faq_question）</label>' +
      '<textarea rows="2" data-k="q">' + esc(item.faq_question) + '</textarea>' +
      '<label>答案（faq_answer）</label>' +
      '<textarea rows="4" data-k="a">' + esc(item.faq_answer) + '</textarea>' +
      '<div class="ops">' +
      '  <button class="btn" data-act="save">保存</button>' +
      '  <button class="btn plain" data-act="cancel">取消</button>' +
      '</div>';

    box.querySelector('[data-act=save]').addEventListener('click', async () => {
      const question = box.querySelector('[data-k=q]').value.trim();
      const answer = box.querySelector('[data-k=a]').value.trim();
      if (!question && !answer) { toast('无改动'); return; }
      try {
        await App.fetchJson(App.PATHS.candidateAction(item.id, 'edit'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ faq_question: question || null, faq_answer: answer || null })
        });
        toast('已保存，重新加载');
        reload();
      } catch (e) { toast('编辑失败：' + e.message); }
    });
    box.querySelector('[data-act=cancel]').addEventListener('click', () => reload());
    card.querySelector('.ops').after(box);
    box.querySelector('[data-k=q]').focus();
  }

  function actButton(ops, label, cls, onClick) {
    const button = App.create('button', 'btn ' + cls, label);
    button.addEventListener('click', () => onClick(button));
    ops.appendChild(button);
    return button;
  }

  function renderOps(ops, item, card) {
    if (item.status === 'draft') {
      actButton(ops, '✔ 通过', 'good', async (btn) => {
        btn.disabled = true;
        try {
          await App.fetchJson(App.PATHS.candidateAction(item.id, 'approve'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: '' })
          });
          toast('已审批通过并入知识库');
          reload();
        } catch (e) { btn.disabled = false; toast('通过失败：' + e.message); }
      });
      actButton(ops, '✕ 驳回', 'bad', async (btn) => {
        if (!(await App.confirm('确认驳回该候选？', { confirmText: '确认驳回', danger: true }))) return;
        btn.disabled = true;
        try {
          await App.fetchJson(App.PATHS.candidateAction(item.id, 'reject'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: '' })
          });
          toast('已驳回');
          reload();
        } catch (e) { btn.disabled = false; toast('驳回失败：' + e.message); }
      });
      actButton(ops, '✎ 编辑', 'plain', () => beginEdit(item, card));
      return;
    }
    if (item.status === 'need_info') {
      // 无证据 / 生成答案不含事实：不允许直接通过，先编辑补充真实答案
      ops.appendChild(App.create('span', 'greet', '该候选没有可用事实，请先「编辑」补充答案，保存后即可通过'));
      actButton(ops, '✎ 编辑', 'plain', () => beginEdit(item, card));
      return;
    }
    if (item.status === 'active') {
      ops.appendChild(App.create('span', 'greet', '已写入知识库（active）'));
      // 误批入库（例如「未提及…建议联系官方」这类没有事实的条目）可一键下架
      actButton(ops, '🗑 下架', 'bad', async (btn) => {
        if (!(await App.confirm('确认下架该知识条目？下架后不再参与检索。',
                                { confirmText: '下架', danger: true }))) return;
        btn.disabled = true;
        try {
          await App.fetchJson(App.PATHS.candidateRemove(item.id), { method: 'DELETE' });
          toast('已下架');
          reload();
        } catch (e) { btn.disabled = false; toast('下架失败：' + e.message); }
      });
    } else if (item.status === 'rejected') {
      ops.appendChild(App.create('span', 'greet', '已驳回（rejected）'));
    } else {
      ops.appendChild(App.create('span', 'greet', '该状态无需审批操作'));
    }
  }

  function render(items) {
    listEl.innerHTML = '';
    countEl.textContent = items.length + ' 条';
    if (!items.length) {
      listEl.innerHTML = '<div class="empty">暂无候选数据（可切换上方状态筛选）</div>';
      return;
    }

    items.forEach(item => {
      const card = App.create('div', 'item');
      card.innerHTML =
        '<div class="item-head">' +
        '  <span class="st ' + esc(item.status) + '">' + esc(ST_LABEL[item.status] || item.status) + '</span>' +
        '  <span class="item-id">#' + esc(shortId(item.id)) + '</span>' +
        '  <span class="item-ts">' + esc(fmtTs(item.ts)) + '</span>' +
        '</div>' +
        '<div class="q">' + esc(item.faq_question) + '</div>' +
        '<div class="a">' + esc(item.faq_answer) + '</div>' +
        '<div class="chips">' + chipsHtml(item) + '</div>' +
        '<div class="ops"></div>';
      renderOps(card.querySelector('.ops'), item, card);
      listEl.appendChild(card);
    });
  }

  async function reload() {
    const status = filterEl.value || undefined;
    const query = status ? ('?status=' + encodeURIComponent(status)) : '';
    try {
      const data = await App.fetchJson(App.PATHS.candidates + query);
      render(Array.isArray(data.items) ? data.items : []);
    } catch (e) {
      listEl.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>';
      countEl.textContent = '—';
    }
    // 审批/驳回/下架都会改变闭环计数：列表刷新时一并刷新状态，避免 pill 停在旧数字
    loadStatus();
  }

  filterEl.addEventListener('change', reload);
  document.getElementById('refresh').addEventListener('click', reload);
  reload();
})();
