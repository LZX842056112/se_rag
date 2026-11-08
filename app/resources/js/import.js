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
  // 与服务端 MAX_BATCH_FILES 保持一致；一次请求最多携带的文件数
  const MAX_BATCH_FILES = 20;
  // 进行中的状态：这些记录不允许删除，重试也只在 failed 时开放
  const ACTIVE_STATUSES = ['uploading', 'pending', 'processing'];

  // 失败原因提示：优先显示可读的中文说明，原始错误放在可展开的日志里
  const FAILURE_HINTS = {
    mineru_failed: 'PDF 解析失败。请检查文件是否加密、损坏或页数过多。',
    mineru_timeout: 'PDF 解析超时。请稍后重试，或拆分文件。',
    parse_failed: '解析结果不完整。请重试。',
    split_empty: '文档没有可切分的正文。请检查文件内容。',
    import_busy: '导入任务较多，等待超时。请稍后重试。',
    storage_unavailable: '存储服务不可用。请检查 MinIO、Milvus 或 MongoDB。',
    embedding_failed: '向量生成失败。请稍后重试。',
    item_name_failed: '主体名称识别失败。请稍后重试。',
    index_failed: '写入知识库失败。请检查存储服务。',
    index_count_mismatch: '入库数量不一致。请重试。',
    import_source_missing: '原始文件不存在。请重新上传。',
    policy_metadata_required: '业务政策必须填写版本和未来的失效时间。',
    policy_metadata_expired: '业务政策的版本或有效期已失效。请重新上传。',
    unsupported_type: '文件类型不支持。仅支持 md / pdf。',
    file_too_large: '文件超过大小上限。',
    file_content_mismatch: '文件内容与扩展名不匹配。',
    save_failed: '文件保存失败。请重试。',
    invalid_filename: '文件名不合法。',
    too_many_files: '一次上传文件过多。'
  };

  const dropZone = document.getElementById('dropZone');
  const fileInput = document.getElementById('fileInput');
  const fileList = document.getElementById('fileList');
  const tokenInput = document.getElementById('importToken');
  const saveTokenBtn = document.getElementById('saveToken');
  const clearRecordsBtn = document.getElementById('clearRecords');

  clearRecordsBtn.addEventListener('click', clearRecords);

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
    const active = new Set(ACTIVE_STATUSES);
    const rows = Array.from(taskRecords.values()).reverse().filter(row => active.has(row.status) || ++finished <= 50);
    try { sessionStorage.setItem(TASKS_KEY, JSON.stringify(rows)); } catch (_) { warnStorage(); }
  }

  function remember(itemEl, patch) {
    const record = taskRecords.get(itemEl.id);
    if (!record) return;
    Object.assign(record, patch);
    saveTasks();
    syncActions(itemEl);
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
    const selected = Array.from(files);
    if (!selected.length) return;
    uploadBatch(selected);
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

  // 服务端消息通常更具体（例如点名哪个文件）。任务失败时优先用中文提示，
  // 因为原始异常文本对用户不可读，细节仍放在可展开的日志里。
  function failureText(errorCode, message, preferHint) {
    const hint = errorCode ? FAILURE_HINTS[errorCode] : '';
    if (preferHint) return hint || message || errorCode || '导入失败';
    return message || hint || errorCode || '导入失败';
  }

  // 失败原因常驻显示，用户不用展开日志就能看到原因；空字符串表示隐藏
  function setFailureReason(itemEl, text) {
    const box = itemEl.querySelector('.failure-reason');
    if (!box) return;
    const textEl = box.querySelector('.failure-reason-text');
    if (!text) {
      box.hidden = true;
      if (textEl) textEl.textContent = '';
      return;
    }
    if (textEl) textEl.textContent = text;
    box.hidden = false;
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
          setFailureReason(itemEl, '无法读取任务状态。请确认导入服务正常，并检查导入令牌。');
          remember(itemEl, { status: 'unavailable' });
          stop();
          return;
        }
        remember(itemEl, { status: data.status, done_list: data.done_list || [], running_list: data.running_list || [],
          error_code: data.error_code || '', message: data.message || '' });
        renderLogs(itemEl, data.done_list, data.running_list);
        if (data.status === 'completed') {
          setBadge(itemEl, '已完成', 'status-completed');
          setFailureReason(itemEl, '');
          updateProgressBar(itemEl.querySelector('.progress-bar'), 100);
          stop();
        } else if (data.status === 'pending') {
          // 任务在进程内队列里等待执行，继续轮询，不能当成失败
          setBadge(itemEl, '排队中...', 'status-processing');
          setFailureReason(itemEl, '');
        } else if (data.status === 'processing') {
          // 进度由日志反映，不造假百分比
          setBadge(itemEl, '处理中...', 'status-processing');
          setFailureReason(itemEl, '');
        } else if (data.status === 'failed') {
          setBadge(itemEl, '失败', 'status-error');
          setFailureReason(itemEl, failureText(data.error_code, data.message, true));
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
          setFailureReason(itemEl, '导入令牌无效或未配置。请保存正确的导入令牌。');
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

  function failItem(itemEl, logSummary, logListEl, logDetails, message, status = 'failed', errorCode = '', preferHint = false) {
    setBadge(itemEl, status === 'interrupted' ? '上传中断' : '失败', 'status-error');
    setFailureReason(itemEl, failureText(errorCode, message, preferHint));
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
          <div class="failure-reason" hidden>
            <span class="failure-reason-title">失败原因</span>
            <span class="failure-reason-text"></span>
          </div>
          <details class="log-details">
            <summary>日志（点击展开）</summary>
            <ul class="log-list"></ul>
          </details>
        </div>
        <div class="item-side">
          <div class="status-badge status-uploading">上传中...</div>
          <div class="item-actions"></div>
        </div>
      </div>
    `);
    const itemEl = document.getElementById(id);
    buildActions(itemEl);
    syncActions(itemEl);
    return itemEl;
  }

  function buildActions(itemEl) {
    const actions = itemEl.querySelector('.item-actions');
    if (!actions) return;

    const retry = App.create('button', 'record-btn', '重试');
    retry.type = 'button';
    retry.dataset.act = 'retry';
    retry.addEventListener('click', () => retryRecord(itemEl));

    const remove = App.create('button', 'record-btn danger', '删除');
    remove.type = 'button';
    remove.dataset.act = 'remove';
    remove.addEventListener('click', () => confirmRemove(itemEl));

    actions.appendChild(retry);
    actions.appendChild(remove);
  }

  // 操作按钮只有在「可执行」时才显示：上传/处理中不显示任何按钮，避免误删正在跑的任务
  function syncActions(itemEl) {
    const record = taskRecords.get(itemEl.id);
    const actions = itemEl.querySelector('.item-actions');
    if (!record || !actions) return;

    const active = ACTIVE_STATUSES.includes(record.status);
    const retryBtn = actions.querySelector('[data-act="retry"]');
    const removeBtn = actions.querySelector('[data-act="remove"]');
    if (retryBtn) retryBtn.hidden = active || record.status !== 'failed' || !record.task_id;
    if (removeBtn) removeBtn.hidden = active;
  }

  function removeRecord(itemEl) {
    const record = taskRecords.get(itemEl.id);
    if (record && record.task_id && pollTimers.has(record.task_id)) {
      clearInterval(pollTimers.get(record.task_id));
      pollTimers.delete(record.task_id);
    }
    taskRecords.delete(itemEl.id);
    itemEl.remove();
    saveTasks();
    refreshEmptyHint();
  }

  async function retryRecord(itemEl) {
    const record = taskRecords.get(itemEl.id);
    if (!record || !record.task_id || record.status !== 'failed') return;

    try {
      const result = await App.fetchJson(App.PATHS.retryImport(record.task_id), {
        method: 'POST', token: currentToken()
      });
      // 服务端沿用原 task_id：进度归零后重新轮询，前端不必更换地址
      remember(itemEl, { status: 'pending', done_list: [], running_list: [], error_code: '', message: '' });
      setBadge(itemEl, '排队中...', 'status-processing');
      setFailureReason(itemEl, '');
      renderLogs(itemEl, [], []);
      updateProgressBar(itemEl.querySelector('.progress-bar'), 0);
      App.toast((result && result.message) || '已重新提交导入');
      pollStatus(record.task_id, itemEl);
    } catch (error) {
      console.warn(error);
      if (needsImportToken(error)) highlightTokenInput();
      App.toast((error && error.message) || '重试失败');
    }
  }

  async function confirmRemove(itemEl) {
    const record = taskRecords.get(itemEl.id);
    if (!record) return;
    if (ACTIVE_STATUSES.includes(record.status)) {
      App.toast('任务进行中，暂不能删除');
      return;
    }
    if (!(await App.confirm(`确认删除「${record.name}」的导入记录？`, { confirmText: '删除', danger: true }))) return;
    await deleteRecord(itemEl, false);
  }

  // silent=true 时用于「清空记录」批量删除：不逐条确认，失败原因由调用方汇总
  async function deleteRecord(itemEl, silent) {
    const record = taskRecords.get(itemEl.id);
    if (!record) return true;
    if (ACTIVE_STATUSES.includes(record.status)) return false;

    if (record.task_id) {
      try {
        await App.fetchJson(App.PATHS.importRecord(record.task_id), {
          method: 'DELETE', token: currentToken()
        });
      } catch (error) {
        console.warn(error);
        if (needsImportToken(error)) highlightTokenInput();
        // 服务端已无这条记录时仍允许清理本地展示（404/410 属幂等删除）
        if (!(error && (error.status === 404 || error.status === 410))) {
          if (!silent) App.toast((error && error.message) || '删除失败');
          return false;
        }
      }
    }
    removeRecord(itemEl);
    return true;
  }

  async function clearRecords() {
    const records = Array.from(taskRecords.values());
    const removable = records.filter(record => !ACTIVE_STATUSES.includes(record.status));
    if (!removable.length) {
      App.toast(records.length ? '进行中的任务不能删除，请等待完成' : '暂无导入记录');
      return;
    }
    if (!(await App.confirm(`确认删除全部 ${removable.length} 条导入记录？`, {
      confirmText: '全部删除', danger: true
    }))) return;

    let failed = 0;
    for (const record of removable) {
      const itemEl = document.getElementById(record.id);
      if (!itemEl) continue;
      if (!(await deleteRecord(itemEl, true))) failed += 1;
    }

    const kept = taskRecords.size;
    if (failed) App.toast(`有 ${failed} 条记录删除失败，请稍后重试`);
    else if (kept) App.toast('已删除可删除的记录，进行中的任务保留');
    else App.toast('导入记录已清空');
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
            itemEl.querySelector('.log-details'), record.message || record.error_code || '导入失败',
            'failed', record.error_code, true);
        } else {
          const completed = record.status === 'completed';
          const queued = record.status === 'pending';
          setBadge(itemEl, completed ? '已完成' : (queued ? '排队中...' : '处理中...'),
            completed ? 'status-completed' : 'status-processing');
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

  async function uploadBatch(files) {
    if (files.length > MAX_BATCH_FILES) {
      App.toast(`一次最多上传 ${MAX_BATCH_FILES} 个文件`);
      return;
    }

    // 先按选择顺序建记录；响应里的 task_ids 也按同一顺序返回
    const items = files.map(file => {
      const record = { id: 'file-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 11),
        name: file.name, size: file.size, task_id: '', status: 'uploading', done_list: [], running_list: [] };
      const itemEl = createTaskItem(record);
      return {
        itemEl,
        progressContainer: itemEl.querySelector('.progress-bar-container'),
        logDetails: itemEl.querySelector('.log-details'),
        logSummary: itemEl.querySelector('.log-details summary'),
        logListEl: itemEl.querySelector('.log-list')
      };
    });
    saveTasks();

    // 本地先做大小校验：任一文件超限即整批不发送，与服务端「整批拒绝」一致
    const oversize = files.find(file => file.size > MAX_UPLOAD_BYTES);
    if (oversize) {
      const message = `文件过大（上限 ${Math.round(MAX_UPLOAD_BYTES / 1024 / 1024)}MB）：${oversize.name}`;
      items.forEach(item => failItem(item.itemEl, item.logSummary, item.logListEl, item.logDetails, message));
      refreshEmptyHint();
      return;
    }

    const formData = new FormData();
    files.forEach(file => formData.append('files', file));
    const kind = document.getElementById('documentKind').value;
    formData.append('document_kind', kind);
    formData.append('document_version', document.getElementById('documentVersion').value.trim());
    const until = document.getElementById('policyUntil').value;
    if (until) formData.append('valid_until', String(new Date(until).getTime() / 1000));

    try {
      items.forEach(item => { item.progressContainer.style.display = 'block'; });
      const result = await App.fetchJson(App.PATHS.upload, {
        method: 'POST', body: formData, token: currentToken()
      });
      const ids = Array.isArray(result.task_ids) ? result.task_ids : [];
      if (!ids.length || ids.length !== files.length) throw new Error('上传接口返回的任务数量与文件数量不一致');
      items.forEach((item, index) => {
        remember(item.itemEl, { task_id: ids[index], status: 'pending' });
        setBadge(item.itemEl, '排队中...', 'status-processing');
        pollStatus(ids[index], item.itemEl);
      });
    } catch (error) {
      // 业务拒绝（如 422 整批含非法文件）属预期路径，用 warn 避免污染错误监控
      console.warn(error);
      if (needsImportToken(error)) highlightTokenInput();
      const message = (error && error.message) ? error.message : String(error);
      items.forEach(item => failItem(item.itemEl, item.logSummary, item.logListEl, item.logDetails,
        message, 'failed', error && error.code));
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
