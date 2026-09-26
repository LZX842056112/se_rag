/*
 * 前后端「查询 / 审批 / 导入」三页共用工具库。
 * 入页面顺序：<script src="/js/common.js"></script> 需先于本页内联 <script>。
 * 各页仅保留自身的 API_BASE fallback 端口（查询/审批 8001，导入 8000）。
 */
(function () {
  'use strict';

  window.resolveApiBase = function (fallbackPort) {
    return location.origin.startsWith('http')
      ? location.origin
      : 'http://127.0.0.1:' + (fallbackPort || 8001);
  };

  // JSON 请求默认头
  window.DEFAULT_HEADERS = { 'Content-Type': 'application/json' };

  window.escapeHtml = function (str) {
    return String(str)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;');
  };

  window.nowTime = function () {
    const d = new Date();
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  };

  // ts: 秒级时间戳；为空或不合法回退当前时刻
  window.formatTime = function (ts) {
    if (!ts) return window.nowTime();
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return window.nowTime();
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  };
  window.formatTs = window.formatTime;

  // 轻量 toast：自动建节点并按时长隐藏，复用同一节点
  window.showToast = function (msg, ms) {
    const duration = ms || 2500;
    let t = document.getElementById('app-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'app-toast';
      t.style.cssText =
        'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);' +
        'background:rgba(20,24,33,.92);color:#fff;padding:8px 16px;border-radius:8px;' +
        'z-index:9999;font-size:13px;box-shadow:0 4px 16px rgba(0,0,0,.18);display:none;';
      document.body.appendChild(t);
    }
    t.textContent = String(msg || '');
    t.style.display = 'block';
    clearTimeout(t._timer);
    t._timer = setTimeout(function () { t.style.display = 'none'; }, duration);
  };
})();