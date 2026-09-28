/*
 * 文件导入页逻辑（公共能力见 /static/app.js）。
 */
(function () {
  'use strict';

  App.configure({ fallbackPort: 8000 });

  const dropZone = document.getElementById('dropZone');
  const fileInput = document.getElementById('fileInput');
  const fileList = document.getElementById('fileList');

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

  // 轮询句柄登记，页面卸载时统一清理防泄漏
  const pollTimers = new Set();

  function pollStatus(taskId, itemEl) {
    const stop = (interval) => { clearInterval(interval); pollTimers.delete(interval); };
    const interval = setInterval(async () => {
      try {
        const data = await App.fetchJson(App.PATHS.taskStatus(taskId));
        renderLogs(itemEl, data.done_list, data.running_list);
        if (data.status === 'completed') {
          setBadge(itemEl, '已完成', 'status-completed');
          updateProgressBar(itemEl.querySelector('.progress-bar'), 100);
          stop(interval);
        } else if (data.status === 'processing') {
          // 进度由日志反映，不造假百分比
          setBadge(itemEl, '处理中...', 'status-processing');
        } else if (data.status === 'failed') {
          setBadge(itemEl, '失败', 'status-error');
          stop(interval);
        }
      } catch (e) {
        console.error('轮询任务状态失败', e);
        stop(interval);
      }
    }, 2000);
    pollTimers.add(interval);
  }

  async function uploadFile(file) {
    const id = 'file-' + Math.random().toString(36).slice(2, 11);
    fileList.insertAdjacentHTML('afterbegin', `
      <div class="file-item" id="${id}">
        <div class="file-info">
          <span class="file-name">${App.escapeHtml(file.name)}</span>
          <span class="file-size">${(file.size / 1024).toFixed(2)} KB</span>
          <div class="progress-bar-container"><div class="progress-bar"></div></div>
          <details class="log-details">
            <summary>日志（点击展开）</summary>
            <ul class="log-list"></ul>
          </details>
        </div>
        <div class="status-badge status-uploading">上传中...</div>
      </div>
    `);

    const itemEl = document.getElementById(id);
    const progressContainer = itemEl.querySelector('.progress-bar-container');
    const logDetails = itemEl.querySelector('.log-details');
    const logSummary = itemEl.querySelector('.log-details summary');
    const logListEl = itemEl.querySelector('.log-list');
    const formData = new FormData();
    formData.append('files', file);

    try {
      progressContainer.style.display = 'block';
      const result = await App.fetchJson(App.PATHS.upload, { method: 'POST', body: formData });
      const ids = Array.isArray(result.task_ids) ? result.task_ids : [];
      if (!ids.length) throw new Error('上传接口未返回 task_id');
      setBadge(itemEl, '处理中...', 'status-processing');
      pollStatus(ids[0], itemEl);
    } catch (error) {
      // 业务拒绝（如 422 仅支持 md/pdf）属预期路径，用 warn 避免污染错误监控
      console.warn(error);
      setBadge(itemEl, '失败', 'status-error');
      logSummary.textContent = '日志（失败，点击展开）';
      logListEl.innerHTML = '';
      logListEl.appendChild(App.create('li', null, (error && error.message) ? error.message : String(error)));
      logDetails.open = true;
    } finally {
      refreshEmptyHint();
    }
  }

  window.addEventListener('pagehide', () => {
    pollTimers.forEach(clearInterval);
    pollTimers.clear();
  });

  refreshEmptyHint();
})();
