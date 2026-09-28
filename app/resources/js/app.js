/*
 * 掌柜智库前端公共库：统一接口地址、请求封装（含统一错误解析）、SSE 封装、
 * 文本格式化与轻提示。三页共用，需先于页面内联脚本加载：
 *   <script src="/static/app.js"></script>
 *
 * 页面初始化示例：
 *   const API_BASE = App.configure({ fallbackPort: 8001 });
 */
(function () {
  'use strict';

  // 统一接口路径（与后端 app/api/routers 一一对应）
  var PATHS = {
    health: '/api/health',
    query: '/api/query',
    stream: function (sessionId) { return '/api/stream/' + encodeURIComponent(sessionId); },
    history: function (sessionId) { return '/api/history/' + encodeURIComponent(sessionId); },
    feedback: '/api/evolution/feedback',
    candidates: '/api/evolution/candidates',
    candidateAction: function (id, action) {
      return '/api/evolution/candidates/' + encodeURIComponent(id) + '/' + action;
    },
    upload: '/api/import/upload',
    taskStatus: function (taskId) { return '/api/import/status/' + encodeURIComponent(taskId); }
  };

  var state = {
    base: '',
    // 审批写操作令牌：仅当前标签页会话有效
    adminToken: sessionStorage.getItem('evo_admin_token') || ''
  };

  /** 同源部署时直接用当前源；单独打开页面（file://）时回退到指定默认端口 */
  function resolveBase(fallbackPort) {
    return location.origin.indexOf('http') === 0
      ? location.origin
      : 'http://127.0.0.1:' + (fallbackPort || 8001);
  }

  function errorMessage(body, status) {
    if (body && typeof body.message === 'string' && body.message) return body.message;
    if (body && typeof body.detail === 'string' && body.detail) return body.detail;
    if (body && Array.isArray(body.detail)) {
      return body.detail.map(function (item) { return (item && (item.msg || item.message)) || ''; })
        .filter(Boolean).join('；') || ('HTTP ' + status);
    }
    return 'HTTP ' + status;
  }

  /** 统一请求：自动拼 base、解析 JSON、错误转成带 message 的 Error */
  async function fetchJson(path, options) {
    var opts = Object.assign({}, options || {});
    var headers = Object.assign({}, opts.headers || {});
    if (state.adminToken) headers['X-Internal-Token'] = state.adminToken;
    if (opts.body && !(opts.body instanceof FormData) && !headers['Content-Type']) {
      headers['Content-Type'] = 'application/json';
    }
    var res = await fetch(state.base + path, Object.assign({}, opts, { headers: headers }));
    var body = null;
    try { body = await res.json(); } catch (_) { /* 无响应体（204 等） */ }
    if (!res.ok) {
      var err = new Error(errorMessage(body, res.status));
      err.status = res.status;
      err.code = body && body.code;
      throw err;
    }
    return body;
  }

  /**
   * 订阅 SSE：返回 EventSource 实例。
   * handlers 支持 onReady / onProgress / onDelta / onFinal / onError / onClose
   */
  function openStream(sessionId, handlers) {
    var h = handlers || {};
    var es = new EventSource(state.base + PATHS.stream(sessionId));
    var raw = '';

    function parse(e) {
      try { return JSON.parse((e && e.data) || '{}'); } catch (_) { return {}; }
    }

    es.addEventListener('ready', function (e) { if (h.onReady) h.onReady(parse(e)); });
    es.addEventListener('progress', function (e) { if (h.onProgress) h.onProgress(parse(e)); });
    es.addEventListener('delta', function (e) {
      var data = parse(e);
      if (data.delta) raw += data.delta;
      if (h.onDelta) h.onDelta(data, raw);
    });
    es.addEventListener('final', function (e) { if (h.onFinal) h.onFinal(parse(e), raw); });
    es.addEventListener('close', function (e) {
      es.close();
      if (h.onClose) h.onClose(parse(e));
    });
    es.addEventListener('error', function (e) {
      var data = parse(e);
      if (h.onError) h.onError(data, raw);
      es.close();
    });
    return es;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }

  function formatTime(value) {
    if (!value) return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    var date = new Date(Number(value) * 1000);
    if (isNaN(date.getTime())) return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }

  /** 兼容秒级/毫秒级时间戳与 Mongo 扩展 JSON（{$date:...}） */
  function formatDateTime(value) {
    if (value == null) return '';
    var date = new Date(value);
    if (isNaN(date.getTime())) {
      if (value && value.$date != null) date = new Date(value.$date);
      else if (typeof value === 'number') date = new Date(value > 1e11 ? value : value * 1000);
    }
    return isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false });
  }

  /** 轻提示：复用同一节点，超时自动隐藏 */
  function toast(message, duration) {
    var node = document.getElementById('app-toast');
    if (!node) {
      node = document.createElement('div');
      node.id = 'app-toast';
      node.className = 'app-toast';
      document.body.appendChild(node);
    }
    node.textContent = String(message == null ? '' : message);
    node.classList.add('show');
    clearTimeout(node._timer);
    node._timer = setTimeout(function () { node.classList.remove('show'); }, duration || 2500);
  }

  /**
   * 页内确认对话框（替代原生 window.confirm）。
   *
   * 原生 confirm 是浏览器模态，会阻塞渲染进程：自动化点不动、标签页可能直接卡死
   * （联调时实测），因此统一改用页内浮层。返回 Promise<boolean>。
   *
   * 用法：const ok = await App.confirm("确认驳回该候选？", { confirmText: "确认驳回", danger: true });
   */
  function confirmDialog(message, options) {
    var opts = options || {};
    return new Promise(function (resolve) {
      var mask = document.createElement('div');
      mask.className = 'app-confirm-mask';
      var box = document.createElement('div');
      box.className = 'app-confirm';
      var text = document.createElement('div');
      text.className = 'app-confirm-text';
      text.textContent = String(message == null ? '' : message);
      var actions = document.createElement('div');
      actions.className = 'app-confirm-actions';

      var cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'btn plain';
      cancelBtn.textContent = opts.cancelText || '取消';
      var okBtn = document.createElement('button');
      okBtn.type = 'button';
      okBtn.className = 'btn' + (opts.danger ? ' bad' : '');
      okBtn.textContent = opts.confirmText || '确定';

      function close(result) {
        document.removeEventListener('keydown', onKey);
        mask.remove();
        resolve(result);
      }
      function onKey(event) { if (event.key === 'Escape') close(false); }

      cancelBtn.addEventListener('click', function () { close(false); });
      okBtn.addEventListener('click', function () { close(true); });
      mask.addEventListener('click', function (event) { if (event.target === mask) close(false); });
      document.addEventListener('keydown', onKey);

      actions.appendChild(cancelBtn);
      actions.appendChild(okBtn);
      box.appendChild(text);
      box.appendChild(actions);
      mask.appendChild(box);
      document.body.appendChild(mask);
      okBtn.focus();
    });
  }

  window.App = {
    PATHS: PATHS,
    resolveBase: resolveBase,
    configure: function (options) {
      var opts = options || {};
      state.base = opts.apiBase || resolveBase(opts.fallbackPort);
      return state.base;
    },
    get base() { return state.base; },
    fetchJson: fetchJson,
    openStream: openStream,
    escapeHtml: escapeHtml,
    formatTime: formatTime,
    formatDateTime: formatDateTime,
    toast: toast,
    confirm: confirmDialog,
    setAdminToken: function (token) {
      state.adminToken = String(token || '').trim();
      try { sessionStorage.setItem('evo_admin_token', state.adminToken); } catch (_) { /* 忽略隐私模式异常 */ }
    },
    getAdminToken: function () { return state.adminToken; }
  };
})();
