/*
 * 客服对话页逻辑（公共能力见 /static/app.js）。
 * 依赖：App.configure / fetchJson / openStream / confirm / toast / renderAnswerWithImages ...
 */
(function () {
  'use strict';

  App.configure({ fallbackPort: 8001 });

  // 页面内短名统一指向公共库实现（避免各页重复实现格式化/转义/提示）
  const escapeHtml = App.escapeHtml;
  const formatTime = App.formatTime;
  const nowTime = () => App.formatTime(null);
  const showToast = App.toast;

  /** 只接受 http/https 链接，避免把 javascript:/data: 之类写进 href */
  function safeHttpUrl(value) {
    const url = String(value || '').trim();
    return /^https?:\/\//i.test(url) ? url : '';
  }

  const chatEl = document.getElementById('chat');
  const inputEl = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const apiPill = document.getElementById('apiPill');
  const btnClear = document.getElementById('btnClear');
  const streamToggle = document.getElementById('streamToggle');

  // 会话 ID：存 localStorage，刷新后沿用同一会话（历史回显依赖它）
  let sessionId = localStorage.getItem('kb_session_id');
  if (!sessionId) {
    sessionId = 'sess-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
    localStorage.setItem('kb_session_id', sessionId);
  }

  let scrollRaf = 0;
  function scrollToBottom() {
    if (scrollRaf) return;
    scrollRaf = requestAnimationFrame(() => {
      scrollRaf = 0;
      chatEl.scrollTop = chatEl.scrollHeight;
    });
  }

  function addUserMsg(text) {
    chatEl.insertAdjacentHTML('beforeend', `
      <div class="msg user">
        <div>
          <div class="bubble">${escapeHtml(text)}</div>
          <div class="meta">${nowTime()}</div>
        </div>
        <div class="avatar">我</div>
      </div>
    `);
    scrollToBottom();
  }

  function addUserMsgWithTime(text, ts) {
    chatEl.insertAdjacentHTML('beforeend', `
      <div class="msg user">
        <div>
          <div class="bubble">${escapeHtml(text)}</div>
          <div class="meta">${formatTime(ts)}</div>
        </div>
        <div class="avatar">我</div>
      </div>
    `);
    scrollToBottom();
  }

  function addBotMsgWithTime(text, ts, imageUrls, citations, groundedness, query, itemNames) {
    const id = 'bot-his-' + Math.random().toString(36).slice(2);
    chatEl.insertAdjacentHTML('beforeend', `
      <div class="msg bot" id="${id}">
        <div class="avatar bot">掌柜智库</div>
        <div>
          <div class="bubble"><div class="answer"></div></div>
          <div class="meta">${formatTime(ts)}</div>
        </div>
      </div>
    `);
    const el = document.getElementById(id);
    if (!el) return;
    App.renderAnswerWithImages(el.querySelector('.answer'), text || '', imageUrls || []);
    // 历史消息同样渲染引用来源 / 置信度 / 反馈，保证刷新后不丢失
    renderCitationsAndFeedback(el, buildBotMeta(citations, groundedness, sessionId, query, itemNames));
    scrollToBottom();
  }

  function addBotMsgSkeleton() {
    const id = 'bot-' + Math.random().toString(36).slice(2);
    chatEl.insertAdjacentHTML('beforeend', `
      <div class="msg bot" id="${id}">
        <div class="avatar bot">掌柜智库</div>
        <div class="msg-body">
          <div class="bubble">
            <span class="typing"><span class="dot"></span><span class="dot"></span><span class="dot"></span></span>
            <details class="progress" open>
              <summary>阶段进度（等待中）</summary>
              <ul></ul>
            </details>
          </div>
          <div class="meta">${nowTime()}</div>
        </div>
      </div>
    `);
    scrollToBottom();
    return document.getElementById(id);
  }

  let lastProgressKey = '';
  function renderProgress(botMsgEl, doneList, runningList, status) {
    const done = Array.isArray(doneList) ? doneList : [];
    const running = Array.isArray(runningList) ? runningList : [];
    const key = status + '|' + done.join(',') + '|' + running.join(',');
    if (key === lastProgressKey) return; // 相同状态短路，避免高频重绘
    lastProgressKey = key;

    const details = botMsgEl.querySelector('details.progress');
    const summary = details.querySelector('summary');
    const ul = details.querySelector('ul');
    const displayStatus = App.STATUS_LABEL[status] || status || 'unknown';

    summary.textContent = `阶段进度（已完成${done.length}，进行中${running.length}，状态：${displayStatus}）`;

    const lines = done.map(x => `✅ ${x}`).concat(running.map(x => `⏳ ${x}`));
    ul.innerHTML = '';
    if (!lines.length) {
      ul.insertAdjacentHTML('beforeend', '<li>暂无进度</li>');
      return;
    }
    lines.forEach(line => ul.insertAdjacentHTML('beforeend', `<li>${escapeHtml(line)}</li>`));
  }

  function buildBotMeta(citations, groundedness, sid, query, itemNames) {
    return {
      citations: Array.isArray(citations) ? citations : [],
      groundedness: (typeof groundedness === 'number' && isFinite(groundedness)) ? groundedness : null,
      sessionId: sid || '',
      query: query || '',
      // 该回答关联的主体：点踩时回传后端，保证缺口/候选能带上正确 item_name
      itemNames: Array.isArray(itemNames) ? itemNames : []
    };
  }

  async function submitFeedback(msgEl, btnEl, meta, value) {
    if (btnEl.dataset.submitted === '1') return;
    btnEl.disabled = true;
    try {
      await App.fetchJson(App.PATHS.feedback, {
        method: 'POST',
        body: JSON.stringify({
          session_id: meta.sessionId,
          query: meta.query,
          // 联网引用没有知识库 id：不能参与缺口/候选信号，否则 URL 会被当成 chunk_id
          cited_chunk_ids: meta.citations
            .filter(c => c && c.source !== 'web' && c.faq_id)
            .map(c => c.faq_id),
          item_names: meta.itemNames || [],
          thumbs: value
        })
      });
      btnEl.dataset.submitted = '1';
      btnEl.classList.add('active');
      const bar = msgEl.querySelector('.feedback-bar');
      if (bar) {
        // 置灰另一枚并标注已反馈
        bar.querySelectorAll('.fb-btn').forEach(b => { if (b !== btnEl) b.disabled = true; });
        if (!bar.querySelector('.fb-done')) bar.appendChild(App.create('span', 'fb-done', '已反馈'));
      }
    } catch (err) {
      btnEl.disabled = false;
      if (btnEl.dataset.submitted !== '1') showToast('反馈提交失败：' + (err && err.message || err));
    }
  }

  function renderCitationsAndFeedback(msgEl, meta) {
    if (!msgEl || !meta) return;
    if (msgEl.querySelector('.cit-wrap')) return; // 防抖：同一消息只渲染一次

    const citations = meta.citations || [];
    const groundedness = meta.groundedness;
    // 说明：反馈栏不因“无引用且无置信度”而隐藏——自进化链路把「无法作答/无引用」的回答
    // 作为重要负反馈来源，必须始终提供点赞/点踩入口。引用块、置信度条仍按各自数据条件渲染。
    const wrap = App.create('div', 'cit-wrap');

    if (citations.length) {
      wrap.appendChild(App.create('div', 'cit-header', `引用来源（${citations.length}）`));
      const list = App.create('div', 'cit-list');
      citations.forEach(c => {
        const source = (c && c.source) || 'kb';
        const label = source === 'evolution' ? '自进化' : (source === 'web' ? '联网' : '知识库');
        const item = App.create('div', 'cit-item');
        item.appendChild(App.create('span', 'cit-tag ' + source, label));
        if (source === 'web') {
          // 联网引用没有 chunk_id：显示标题（无标题则显示 URL），可点开原网页
          const url = safeHttpUrl((c && c.faq_id) || '');
          const text = String((c && c.title) || '').trim() || url;
          if (url) {
            const link = App.create('a', 'cit-id cit-link', text);
            link.href = url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            item.appendChild(link);
          } else {
            item.appendChild(App.create('span', 'cit-id', text));
          }
        } else {
          item.appendChild(App.create('span', 'cit-id', String((c && c.faq_id) || '').slice(0, 24)));
        }
        list.appendChild(item);
      });
      wrap.appendChild(list);
    }

    if (typeof groundedness === 'number' && isFinite(groundedness)) {
      const pct = Math.round(groundedness * 100);
      const gz = App.create('div', 'groundedness');
      const gauge = App.create('div', 'gauge');
      const fill = App.create('i');
      fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
      gauge.appendChild(fill);
      gz.appendChild(gauge);
      gz.appendChild(App.create('span', null, '回答置信度 ' + pct + '%'));
      wrap.appendChild(gz);
    } else {
      // 未评估（证据为空 / 评估失败）时不显示 0%，避免与「确实不接地」混淆
      const gz = App.create('div', 'groundedness unknown');
      gz.appendChild(App.create('span', null, '回答置信度 未评估'));
      wrap.appendChild(gz);
    }

    const bar = App.create('div', 'feedback-bar');
    const up = App.create('button', 'fb-btn', '👍 有帮助');
    up.type = 'button';
    up.addEventListener('click', () => submitFeedback(msgEl, up, meta, 1));
    const down = App.create('button', 'fb-btn', '👎 没帮助');
    down.type = 'button';
    down.addEventListener('click', () => submitFeedback(msgEl, down, meta, -1));
    bar.appendChild(up);
    bar.appendChild(down);
    wrap.appendChild(bar);

    // 插到 .meta 之前（.meta 在骨架的内层容器里，必须用其父节点 insertBefore，
    // 否则抛 NotFoundError 且被静默 catch 吞掉，表现为反馈栏凭空消失）
    const metaEl = msgEl.querySelector('.meta');
    if (metaEl) metaEl.parentNode.insertBefore(wrap, metaEl);
    else msgEl.appendChild(wrap);
  }

  function finalizeBotAnswer(botMsgEl, answer, error, imageUrls, meta) {
    const bubble = botMsgEl.querySelector('.bubble');
    const progress = botMsgEl.querySelector('details.progress');
    const err = (error || '').trim();

    if (err) {
      if (progress) progress.remove();
      bubble.textContent = '';
      bubble.appendChild(App.create('div', 'answer', `抱歉，本次处理失败：\n${err}`));
      return;
    }

    if (progress) {
      progress.remove();
      progress.removeAttribute('open'); // 完成后自动收起
    }
    bubble.textContent = '';
    const answerEl = App.create('div', 'answer');
    bubble.appendChild(answerEl);
    App.renderAnswerWithImages(answerEl, answer || '', imageUrls || []);
    renderCitationsAndFeedback(botMsgEl, meta);
    if (progress) bubble.appendChild(progress);
  }

  let healthFails = 0;
  async function apiHealth() {
    try {
      await App.fetchJson(App.PATHS.health);
      healthFails = 0;
      apiPill.textContent = 'API: 已连接';
      apiPill.style.cursor = 'default';
    } catch (_) {
      healthFails += 1;
      apiPill.textContent = healthFails >= 3 ? 'API: 未连接（点此重连）' : 'API: 未连接';
      apiPill.style.cursor = healthFails >= 3 ? 'pointer' : 'default';
    }
  }
  apiPill.addEventListener('click', () => { if (healthFails >= 3) apiHealth(); });

  async function loadHistory() {
    try {
      const data = await App.fetchJson(App.PATHS.history(sessionId));
      const items = Array.isArray(data.items) ? data.items : [];
      if (!items.length) return;
      // 保留首条欢迎消息，其余先清空再按时间正序渲染
      Array.from(chatEl.querySelectorAll('.msg')).slice(1).forEach(node => node.remove());
      items.reverse();
      let lastUserQuery = '';
      items.forEach(item => {
        if (item.role === 'user') {
          lastUserQuery = item.text || '';
          addUserMsgWithTime(item.text || '', item.ts);
        } else {
          addBotMsgWithTime(item.text || '', item.ts, item.image_urls || [],
                            item.citations || [], item.groundedness, lastUserQuery,
                            item.item_names || []);
        }
      });
      scrollToBottom();
    } catch (err) {
      // 不要静默吞掉异常：历史上「反馈按钮不渲染」正是被静默 catch 掩盖的
      console.error('历史加载失败', err);
      showToast('历史加载失败，请稍后重试');
    }
  }

  function submitQuery(text) {
    return App.fetchJson(App.PATHS.query, {
      method: 'POST',
      body: JSON.stringify({ query: text, session_id: sessionId, is_stream: streamToggle.checked })
    });
  }

  function ensureAnswerEl(botMsgEl) {
    const bubble = botMsgEl.querySelector('.bubble');
    let answerEl = bubble.querySelector('.answer');
    if (!answerEl) {
      answerEl = App.create('div', 'answer');
      bubble.insertBefore(answerEl, bubble.firstChild);
    }
    return answerEl;
  }

  /**
   * 点选主体后该问什么：
   * - 原问题本身就是在指这个主体（如只输入了 “hak180”）→ 直接用主体名提问；
   * - 否则把主体名与原问题拼起来（如 “HAK 180 烫金机 怎么换膜”）。
   */
  function optionQuestion(name, originalQuery) {
    const subject = String(name || '').trim();
    const asked = String(originalQuery || '').trim();
    if (!asked || !subject) return subject || asked;
    const compactAsked = asked.replace(/\s+/g, '').toLowerCase();
    const compactSubject = subject.replace(/\s+/g, '').toLowerCase();
    if (compactAsked.length <= 12 || compactSubject.includes(compactAsked) || compactAsked.includes(compactSubject)) {
      return subject;
    }
    return subject + ' ' + asked;
  }

  function askSubject(name, originalQuery) {
    inputEl.value = optionQuestion(name, originalQuery);
    onSend();
  }

  /** 渲染「没确认到主体」时的相似主体选项：点一下即以该主体重新提问。 */
  function renderItemNameOptions(msgEl, options, originalQuery) {
    const list = (Array.isArray(options) ? options : []).filter(o => o && o.item_name);
    if (!msgEl || !list.length || msgEl.querySelector('.option-bar')) return;
    const bar = App.create('div', 'option-bar');
    bar.appendChild(App.create('span', 'option-hint', '请选择主体：'));
    const seen = new Set();
    list.forEach(option => {
      const name = String(option.item_name);
      if (seen.has(name)) return;
      seen.add(name);
      const btn = App.create('button', 'option-btn', name);
      btn.type = 'button';
      btn.addEventListener('click', () => askSubject(name, originalQuery));
      bar.appendChild(btn);
    });
    const metaEl = msgEl.querySelector('.meta');
    if (metaEl) metaEl.parentNode.insertBefore(bar, metaEl);
    else msgEl.appendChild(bar);
  }

  async function onSend() {
    if (sendBtn.disabled) return; // 按钮禁用时拦截重复提交（含 Enter 快捷键）
    const text = (inputEl.value || '').trim();
    if (!text) return;
    inputEl.value = '';
    addUserMsg(text);
    const botMsgEl = addBotMsgSkeleton();
    sendBtn.disabled = true;

    try {
      const isStream = streamToggle.checked;
      const data = await submitQuery(text);
      renderProgress(botMsgEl, [], [], 'pending');

      if (!isStream) {
        // 非流式：直接渲染完整结果
        renderProgress(botMsgEl, data.done_list || [], [], 'completed');
        finalizeBotAnswer(botMsgEl, data.answer, data.error, data.image_urls || [],
                          buildBotMeta(data.citations, data.groundedness, data.session_id, text,
                                       data.item_names));
        renderItemNameOptions(botMsgEl, data.item_name_options, text);
        sendBtn.disabled = false;
        return;
      }

      // 流式：SSE 订阅与事件分发统一由公共库处理
      const answerEl = ensureAnswerEl(botMsgEl);
      App.openStream(data.session_id, {
        onProgress(d) {
          renderProgress(botMsgEl, d.done_list, d.running_list, d.status);
          // 进度已 completed 时提前结束等待态，避免后端未发 final 时按钮一直禁用
          if (d && d.status === 'completed') {
            const typing = botMsgEl.querySelector('.typing');
            if (typing) typing.remove();
            sendBtn.disabled = false;
          }
        },
        onDelta(d, rawText) {
          App.renderAnswerWithImages(answerEl, rawText, []);
          scrollToBottom();
        },
        onFinal(d, rawText) {
          const typing = botMsgEl.querySelector('.typing');
          if (typing) typing.remove();
          const progress = botMsgEl.querySelector('details.progress');
          if (progress) progress.removeAttribute('open');
          // 流式 delta 可能不含【图片】标记，最终包的 answer 才是完整答案，优先使用它
          const hasFinal = d && typeof d.answer === 'string' && d.answer.trim().length > 0;
          App.renderAnswerWithImages(answerEl,
                                     hasFinal ? d.answer : (rawText || answerEl.textContent || ''),
                                     (d && d.image_urls) || []);
          renderCitationsAndFeedback(botMsgEl, buildBotMeta(
            d && d.citations, d && d.groundedness, sessionId, text, d && d.item_names));
          renderItemNameOptions(botMsgEl, d && d.item_name_options, text);
          sendBtn.disabled = false;
        },
        onClose() { sendBtn.disabled = false; },
        onError(d, rawText) {
          const typing = botMsgEl.querySelector('.typing');
          if (typing) typing.remove();
          const progress = botMsgEl.querySelector('details.progress');
          if (progress) progress.removeAttribute('open');
          const msg = (d && (d.message || d.error)) || 'SSE 连接中断/失败';
          App.renderAnswerWithImages(answerEl, `${rawText}\n\n（错误：${msg}）`, []);
          sendBtn.disabled = false;
        }
      });
    } catch (e) {
      const bubble = botMsgEl.querySelector('.bubble');
      const progress = botMsgEl.querySelector('details.progress');
      const typing = botMsgEl.querySelector('.typing');
      if (typing) typing.remove();
      bubble.innerHTML = `请求失败：${escapeHtml(e.message || e)}\n\n` + (progress ? progress.outerHTML : '');
      sendBtn.disabled = false;
    }
  }

  sendBtn.addEventListener('click', onSend);
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      onSend();
    }
  });

  btnClear.addEventListener('click', async () => {
    const ok = await App.confirm('确定要清空当前会话的历史记录吗？这将无法恢复。',
                                { confirmText: '清空', danger: true });
    if (!ok) return;
    try {
      await App.fetchJson(App.PATHS.history(sessionId), { method: 'DELETE' });
    } catch (_) {
      showToast('服务端清空失败，仅清空本地显示');
    }
    // 清空除首条欢迎消息外的内容
    Array.from(chatEl.querySelectorAll('.msg')).slice(1).forEach(node => node.remove());
    scrollToBottom();
  });

  apiHealth();
  setInterval(apiHealth, 5000);
  loadHistory();
  setTimeout(() => inputEl.focus(), 200);
})();
