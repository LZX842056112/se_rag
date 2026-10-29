/*
 * 文件导入页逻辑（公共能力见 /static/app.js）。
 */
(function () {
  'use strict';

  App.configure({ fallbackPort: 8000 });

  const TASKS_KEY = 'import_tasks_v1:' + App.base;
  const taskRecords = new Map();
  const pollTimers = new Map();
  let storageWarningShown = false;

  // 与服务端 IMPORT_MAX_UPLOAD_MB 默认值保持一致；本地先拦一次，避免白传超大文件
  const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

  const dropZone = document.getElementById('dropZone');
  const fileInput = document.getElementById('fileInput');
  const fileList = document.getElementById('fileList');
  const tokenInput = document.getElementById('importToken');
  const saveTokenBtn = document.getElementById('saveToken');

  // 导入令牌（IMPORT_ADMIN_TOKEN）：仅当前标签页会话有效，与审批令牌相互独立
  tokenInput.value = App.getImportToken();
  saveTokenBtn.addEventListener('click', () => {
    App.setImportToken(tokenInput.value);
    tokenInput.classList.remove('attention');
    App.toast('导入令牌已保存');
    resumePolling(true);
  });

  function currentToken() {
    return App.getImportToken();
  }

  function highlightTokenInput() {
    tokenInput.classList.add('attention');
    tokenInput.focus();
  }

  function needsImportToken(error) {
    return error && (error.status === 401 || (error.status === 503 && error.code === 'import_admin_token_missing'));
  }

  function warnStorage() {
    if (storageWarningShown) return;
    storageWarningShown = true;
    App.toast('浏览器无法保存导入任务，刷新后可能无法恢复。');
  }

  function saveTasks() {
    let finished = 0;
    const active = new Set(['uploading', 'pending', 'processing']);
    const rows = Array.from(taskRecords.values()).reverse().filter(row => active.has(row.status) || ++finished <= 50);
    try { sessionStorage.setItem(TASKS_KEY, JSON.stringify(rows)); } catch (_) { warnStorage(); }
  }

  function remember(itemEl, patch) {
    const record = taskRecords.get(itemEl.id);
    if (!record) return;
    Object.assign(record, patch);
    saveTasks();
  }

  function savedTasks() {
    let raw, rows;
    try { raw = sessionStorage.getItem(TASKS_KEY); } catch (_) { warnStorage(); return []; }
    try { rows = JSON.parse(raw || '[]'); } catch (_) { return []; }
    if (!Array.isArray(rows)) return [];
    return rows.filter(row => row && /^file-[a-z0-9]+$/i.test(row.id) && typeof row.name === 'string'
      && typeof row.size === 'number' && Number.isFinite(row.size) && row.size >= 0
      && (!row.task_id || (typeof row.task_id === 'string' && /^[a-z0-9_-]{1,128}$/i.test(row.task_id))))
      .map(row => ({ id: row.id, name: row.name, size: row.size, task_id: row.task_id || '',
        status: typeof row.status === 'string' ? row.status : 'pending',
        done_list: Array.isArray(row.done_list) ? row.done_list.filter(text => typeof text === 'string') : [],
        running_list: Array.isArray(row.running_list) ? row.running_list.filter(text => typeof text === 'string') : [],
        error_code: typeof row.error_code === 'string' ? row.error_code : '',
        message: typeof row.message === 'string' ? row.message : '' }));
  }

  dropZone.addEventListener('click', () => fileInput.click());
  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
  });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    handleFiles(e.dataTransfer.files);
  });
  fileInput.addEventListener('change', (e) => handleFiles(e.target.files));

  function handleFiles(files) {
    Array.from(files).forEach(uploadFile);
  }

  // 任务区空态提示
  function refreshEmptyHint() {
    if (fileList.children.length === 0) {
      fileList.innerHTML = '<div class="empty-hint">暂无导入任务</div>';
      return;
    }
    const hint = fileList.querySelector('.empty-hint');
    if (hint) hint.remove();
  }

  function updateProgressBar(bar, percentage) {
    bar.style.width = percentage + '%';
    bar.style.backgroundColor = percentage >= 100 ? 'var(--brand)' : '#f2a33c';
  }

  function setBadge(itemEl, text, cls) {
    const badge = itemEl.querySelector('.status-badge');
    badge.textContent = text;
    badge.className = 'status-badge ' + cls;
  }

  function normalizeDoneLog(text) {
    // 后端可能返回中文节点名，也可能已拼接“已完成”
    if (typeof text !== 'string') return String(text);
    return text.endsWith('已完成') ? text : `${text}已完成`;
  }

  function normalizeRunningLog(text) {
    // 后端可能返回中文节点名，也可能已拼接“正在进行.../处理中...”
    if (typeof text !== 'string') return String(text);
    if (text.startsWith('正在进行')) return text.endsWith('...') ? text : `${text}...`;
    return `正在进行${text}...`;
  }

  function renderLogs(itemEl, doneList, runningList) {
    const logSummary = itemEl.querySelector('.log-details summary');
    const logListEl = itemEl.querySelector('.log-list');
    const logDetails = itemEl.querySelector('.log-details');
    const done = Array.isArray(doneList) ? doneList : [];
    const running = Array.isArray(runningList) ? runningList : [];

    // 更新 summary（即使收起也能看到进度）
    logSummary.textContent = `日志（已完成${done.length}，进行中${running.length}，点击展开）`;

    const lines = done.map(normalizeDoneLog).concat(running.map(normalizeRunningLog));
    logListEl.innerHTML = '';
    if (!lines.length) {
      logListEl.appendChild(App.create('li', null, '暂无日志'));
      logDetails.open = false; // 没有任何日志时默认收起
      return;
    }
    lines.forEach(line => logListEl.appendChild(App.create('li', null, line)));
  }

  function pollStatus(taskId, itemEl) {
    if (pollTimers.has(taskId)) return;
    const current = () => pollTimers.get(taskId) === interval;
    const stop = () => { clearInterval(interval); if (current()) pollTimers.delete(taskId); };
    let inFlight = false;
    const poll = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const data = await App.fetchJson(App.PATHS.taskStatus(taskId), { token: currentToken() });
        if (!current()) return;
        if (!data.status) {
          setBadge(itemEl, '状态不可用', 'status-error');
          remember(itemEl, { status: 'unavailable' });
          stop();
          return;
        }
        remember(itemEl, { status: data.status, done_list: data.done_list || [], running_list: data.running_list || [],
          error_code: data.error_code || '', message: data.message || '' });
        renderLogs(itemEl, data.done_list, data.running_list);
        if (data.status === 'completed') {
          setBadge(itemEl, '已完成', 'status-completed');
          updateProgressBar(itemEl.querySelector('.progress-bar'), 100);
          stop();
        } else if (data.status === 'processing' || data.status === 'pending') {
          // 进度由日志反映，不造假百分比
          setBadge(itemEl, '处理中...', 'status-processing');
        } else if (data.status === 'failed') {
          setBadge(itemEl, '失败', 'status-error');
          if (data.error_code) {
            const logListEl = itemEl.querySelector('.log-list');
            logListEl.innerHTML = '';
            logListEl.appendChild(App.create('li', null, data.message || data.error_code));
            itemEl.querySelector('.log-details').open = true;
          }
          stop();
        }
      } catch (e) {
        if (!current()) return;
        console.error('轮询任务状态失败', e);
        if (needsImportToken(e)) {
          highlightTokenInput();
          setBadge(itemEl, '等待验证导入令牌', 'status-error');
          stop();
        } else {
          const record = taskRecords.get(itemEl.id);
          if (!record || !['completed', 'failed'].includes(record.status)) {
            setBadge(itemEl, '暂时无法读取，正在重试', 'status-processing');
          }
        }
      } finally {
        inFlight = false;
      }
    };
    const interval = setInterval(poll, 2000);
    pollTimers.set(taskId, interval);
    poll();
  }

  function failItem(itemEl, logSummary, logListEl, logDetails, message, status = 'failed') {
    setBadge(itemEl, status === 'interrupted' ? '上传中断' : '失败', 'status-error');
    remember(itemEl, { status, message });
    logSummary.textContent = '日志（失败，点击展开）';
    logListEl.innerHTML = '';
    logListEl.appendChild(App.create('li', null, message));
    logDetails.open = true;
  }

  function createTaskItem(record) {
    const { id, name, size } = record;
    taskRecords.set(id, record);
    fileList.insertAdjacentHTML('afterbegin', `
      <div class="file-item" id="${id}">
        <div class="file-info">
          <span class="file-name">${App.escapeHtml(name)}</span>
          <span class="file-size">${(size / 1024).toFixed(2)} KB</span>
          <div class="progress-bar-container"><div class="progress-bar"></div></div>
          <details class="log-details">
            <summary>日志（点击展开）</summary>
            <ul class="log-list"></ul>
          </details>
        </div>
        <div class="status-badge status-uploading">上传中...</div>
      </div>
    `);
    return document.getElementById(id);
  }

  function resumePolling(restart = false) {
    taskRecords.forEach(record => {
      const itemEl = document.getElementById(record.id);
      if (!record.task_id || !itemEl || ['completed', 'failed'].includes(record.status)) return;
      if (restart && pollTimers.has(record.task_id)) {
        clearInterval(pollTimers.get(record.task_id));
        pollTimers.delete(record.task_id);
      }
      pollStatus(record.task_id, itemEl);
    });
  }

  function restoreTasks() {
    savedTasks().reverse().forEach(record => {
      if (taskRecords.has(record.id)) return;
      const itemEl = createTaskItem(record);
      itemEl.querySelector('.progress-bar-container').style.display = 'block';
      renderLogs(itemEl, record.done_list, record.running_list);
      if (record.task_id) {
        if (record.status === 'failed') {
          failItem(itemEl, itemEl.querySelector('.log-details summary'), itemEl.querySelector('.log-list'),
            itemEl.querySelector('.log-details'), record.message || record.error_code || '导入失败');
        } else {
          setBadge(itemEl, record.status === 'completed' ? '已完成' : '处理中...',
            record.status === 'completed' ? 'status-completed' : 'status-processing');
        }
        if (record.status === 'completed') updateProgressBar(itemEl.querySelector('.progress-bar'), 100);
        if (!['completed', 'failed'].includes(record.status)) pollStatus(record.task_id, itemEl);
      } else {
        const interrupted = record.status === 'uploading' || record.status === 'interrupted';
        failItem(itemEl, itemEl.querySelector('.log-details summary'), itemEl.querySelector('.log-list'),
          itemEl.querySelector('.log-details'), interrupted
            ? '刷新前尚未收到上传结果。请先核对是否已导入；如未导入，请重新选择文件。' : record.message,
          interrupted ? 'interrupted' : 'failed');
      }
    });
  }

  async function uploadFile(file) {
    const record = { id: 'file-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 11),
      name: file.name, size: file.size, task_id: '', status: 'uploading', done_list: [], running_list: [] };
    const itemEl = createTaskItem(record);
    saveTasks();
    const progressContainer = itemEl.querySelector('.progress-bar-container');
    const logDetails = itemEl.querySelector('.log-details');
    const logSummary = itemEl.querySelector('.log-details summary');
    const logListEl = itemEl.querySelector('.log-list');

    // 本地先做大小校验：避免把超大文件白传到服务端再被 413 拒绝
    if (file.size > MAX_UPLOAD_BYTES) {
      failItem(itemEl, logSummary, logListEl, logDetails,
        `文件过大（上限 ${Math.round(MAX_UPLOAD_BYTES / 1024 / 1024)}MB）`);
      refreshEmptyHint();
      return;
    }

    const formData = new FormData();
    formData.append('files', file);
    const kind = document.getElementById('documentKind').value;
    formData.append('document_kind', kind);
    formData.append('document_version', document.getElementById('documentVersion').value.trim());
    const until = document.getElementById('policyUntil').value;
    if (until) formData.append('valid_until', String(new Date(until).getTime() / 1000));

    try {
      progressContainer.style.display = 'block';
      const result = await App.fetchJson(App.PATHS.upload, {
        method: 'POST', body: formData, token: currentToken()
      });
      const ids = Array.isArray(result.task_ids) ? result.task_ids : [];
      if (!ids.length) throw new Error('上传接口未返回 task_id');
      remember(itemEl, { task_id: ids[0], status: 'processing' });
      setBadge(itemEl, '处理中...', 'status-processing');
      pollStatus(ids[0], itemEl);
    } catch (error) {
      // 业务拒绝（如 422 仅支持 md/pdf）属预期路径，用 warn 避免污染错误监控
      console.warn(error);
      if (needsImportToken(error)) highlightTokenInput();
      failItem(itemEl, logSummary, logListEl, logDetails,
        (error && error.message) ? error.message : String(error));
    } finally {
      refreshEmptyHint();
    }
  }

  window.addEventListener('pagehide', () => {
    pollTimers.forEach(clearInterval);
    pollTimers.clear();
  });

  window.addEventListener('pageshow', event => { if (event.persisted) resumePolling(); });

  restoreTasks();
  refreshEmptyHint();
})();
