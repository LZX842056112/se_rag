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
  const btnNew = document.getElementById('btnNew');
  if (btnNew) btnNew.addEventListener('click', () => {
    localStorage.setItem('kb_session_id', 'sess-' + crypto.randomUUID());
    location.reload();
  });
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

  function addBotMsgWithTime(text, ts, imageUrls, citations, groundedness, query, itemNames, intent, answerId, issueId) {
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
    renderCitationsAndFeedback(el, buildBotMeta(citations, groundedness, sessionId, query, itemNames, intent, answerId));
    // 闲聊轮（intent=chitchat）渲染「闲聊」徽标，与知识库作答区分
    if (intent === 'chitchat') renderChitchatBadge(el);
    if (issueId) el.querySelector('.bubble').appendChild(App.create('div', 'option-hint', '已登记待办：' + issueId));
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

  function buildBotMeta(citations, groundedness, sid, query, itemNames, intent, answerId) {
    return {
      citations: Array.isArray(citations) ? citations : [],
      groundedness: (typeof groundedness === 'number' && isFinite(groundedness)) ? groundedness : null,
      sessionId: sid || '',
      intent: intent || 'rag',
      answerId: answerId || '',
      query: query || '',
      // 该回答关联的主体：点踩时回传后端，保证缺口/候选能带上正确 item_name
      itemNames: Array.isArray(itemNames) ? itemNames : []
    };
  }

  async function submitFeedback(msgEl, btnEl, meta, value, resolved) {
    if (btnEl.dataset.submitted === '1') return;
    btnEl.disabled = true;
    try {
      await App.fetchJson(App.PATHS.feedback, {
        method: 'POST',
        body: JSON.stringify({
          session_id: meta.sessionId,
          answer_id: meta.answerId,
          query: meta.query,
          // 联网引用没有知识库 id：不能参与缺口/候选信号，否则 URL 会被当成 chunk_id
          cited_chunk_ids: meta.citations
            .filter(c => c && c.source !== 'web' && c.faq_id)
            .map(c => c.faq_id),
          item_names: meta.itemNames || [],
          thumbs: value,
          resolved: resolved
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
          item.appendChild(App.create('span', 'cit-id', [c.title || c.faq_id, c.heading, c.page ? '第' + c.page + '页' : '', c.version ? '版本 ' + c.version : ''].filter(Boolean).join(' · ')));
        }
        list.appendChild(item);
      });
      wrap.appendChild(list);
    }

    if (meta.intent === 'chitchat') {
      wrap.appendChild(App.create('span', 'groundedness unknown', '日常交流'));
    } else if (typeof groundedness === 'number' && isFinite(groundedness)) {
      const pct = Math.round(groundedness * 100);
      const gz = App.create('div', 'groundedness');
      const gauge = App.create('div', 'gauge');
      const fill = App.create('i');
      fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
      gauge.appendChild(fill);
      gz.appendChild(gauge);
      gz.appendChild(App.create('span', null, '证据支持度 ' + pct + '%'));
      wrap.appendChild(gz);
    } else {
      // 未评估（证据为空 / 评估失败）时不显示 0%，避免与「确实不接地」混淆
      const gz = App.create('div', 'groundedness unknown');
      gz.appendChild(App.create('span', null, '证据支持度 未评估'));
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
    if (meta.intent !== 'chitchat') {
      [ [true, '已解决'], [false, '仍未解决'] ].forEach(([resolved, label]) => {
        const button = App.create('button', 'fb-btn', label);
        button.type = 'button';
        button.addEventListener('click', () => submitFeedback(msgEl, button, meta, null, resolved));
        bar.appendChild(button);
      });
    }
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
      const health = await App.fetchJson(App.PATHS.health);
      // 服务端资源版本变了 → 说明后端已部署新前端，提示刷新（长开页面不会自动换脚本）
      if (App.checkFreshness) App.checkFreshness(health);
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

  /**
   * 回显会话历史。
   *
   * @returns {Promise<{lastRole: string, lastUserText: string}>}
   *   ``lastRole`` 为最后一条消息的角色（无历史时为空串）；``lastUserText`` 为最近一条提问。
   *   刷新恢复流程据此判断「回答是否已落库」，避免把已完成的一轮重复渲染。
   */
  async function loadHistory() {
    try {
      const data = await App.fetchJson(App.PATHS.history(sessionId));
      const items = Array.isArray(data.items) ? data.items : [];
      if (!items.length) return { lastRole: '', lastUserText: '', lastTurnId: '', lastUserTurnId: '' };
      // 保留首条欢迎消息，其余先清空再按时间正序渲染
      Array.from(chatEl.querySelectorAll('.msg')).slice(1).forEach(node => node.remove());
      items.reverse();
      let lastUserQuery = '';
      let lastUserTurnId = '';
      items.forEach(item => {
        if (item.role === 'user') {
          lastUserQuery = item.text || '';
          lastUserTurnId = item.turn_id || '';
          addUserMsgWithTime(item.text || '', item.ts);
        } else {
          addBotMsgWithTime(item.text || '', item.ts, item.image_urls || [],
                            item.citations || [], item.groundedness, lastUserQuery,
                            item.item_names || [], item.intent, item.id, item.issue_id);
        }
      });
      scrollToBottom();
      return {
        lastRole: String(items[items.length - 1].role || ''),
        lastUserText: lastUserQuery,
        lastTurnId: items[items.length - 1].turn_id || '',
        lastUserTurnId: lastUserTurnId
      };
    } catch (err) {
      // 不要静默吞掉异常：历史上「反馈按钮不渲染」正是被静默 catch 掩盖的
      console.error('历史加载失败', err);
      showToast('历史加载失败，请稍后重试');
      return { lastRole: '', lastUserText: '' };
    }
  }

  function submitQuery(text, rewriteHint) {
    const payload = { query: text, session_id: sessionId, is_stream: streamToggle.checked };
    // 仅在点选主体时附带：服务端用它拼出完整问句，避免模型把意图替换成历史话题
    if (rewriteHint) payload.rewrite_hint = rewriteHint;
    return App.fetchJson(App.PATHS.query, { method: 'POST', body: JSON.stringify(payload) });
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

  /** 问题里去掉空白/标点后只剩主体名本身 → 用户想问的就是这个主体，直接用它提问 */
  function isSubjectOnly(asked, subject) {
    const compact = (s) => String(s || '').replace(/[\s\p{P}\p{S}]/gu, '').toLowerCase();
    const askedKey = compact(asked);
    const subjectKey = compact(subject);
    if (!askedKey) return true;
    if (!subjectKey) return false;
    return askedKey === subjectKey
      || subjectKey.includes(askedKey)          // 只输入了型号前缀，如 “hak180”
      || askedKey.includes(subjectKey);         // 已带主体名，无需再拼
  }

  /**
   * 点选主体后该怎么问：优先向服务端带上**结构化意图**，而不是把意图词混进问句。
   *
   * 需求：用户问“hak180怎么安装” → 系统反问主体 → 用户点选主体。
   * 此时前端完全知道「意图 = 怎么安装」，只有主体名缺失。
   * 拆成两个独立信令传给服务端：
   *   - query    = 主体名（含型号别名，便于目录对齐）
   *   - rewrite_hint = 用户点选前的那句原问题（保留意图词，排除历史干扰）
   * 服务端据此拼出 “Brother HAK 180 烫金机怎么安装”，不必让模型去猜。
   *
   * 这样做的原因：只把意图词拼进问句时，模型仍可能被历史对话改写（实测：
   * 纯主体名 + 历史在聊“蓝牙” → 被改写成蓝牙问题）。把意图放进独立字段后，
   * 它不参与“指代消解”的推断，模型没有把它替换掉的动机。
   *
   * 兜底：旧服务端不认识 rewrite_hint 时会被忽略，此时仍靠拼好的问句生效，
   * 故问句本身也保留意图词（与 hint 内容一致，只是多带一次主体名）。
   */
  function optionQuestion(name, originalQuery) {
    const subject = String(name || '').trim();
    const asked = String(originalQuery || '').trim();
    if (!asked || !subject) return subject || asked;
    if (isSubjectOnly(asked, subject)) return subject;
    return subject + ' ' + asked;
  }

  /** 点选主体时要带给服务端的意图提示；纯主体名（无意图可补）时返回空串。 */
  function optionIntentHint(name, originalQuery) {
    const subject = String(name || '').trim();
    const asked = String(originalQuery || '').trim();
    if (!asked || !subject) return '';
    return isSubjectOnly(asked, subject) ? '' : asked;
  }

  function askSubject(name, originalQuery) {
    inputEl.value = optionQuestion(name, originalQuery);
    onSend(optionIntentHint(name, originalQuery));
  }

  /**
   * 挂起的反问：来源问句 + 候选主体名。
   *
   * 用户不点选项按钮、而是自己手输主体名时，用来源问句补 rewrite_hint——
   * 这样手输与点选走同一条服务端确定性拼装路径，不必让模型从历史里猜意图
   * （实测：模型会借更早的话题，表现为「答的是之前的问题」）。
   */
  let pendingSubjectAsk = null; // { sourceQuery: string, names: string[] }

  /** 手输文本是否就是候选中的某个主体（含型号前缀、`hak-180` 这类别名写法）。 */
  function matchesPendingSubject(text, names) {
    const compact = (s) => String(s || '').replace(/[\s\p{P}\p{S}]/gu, '').toLowerCase();
    const askedKey = compact(text);
    if (!askedKey) return false;
    return (Array.isArray(names) ? names : []).some(name => {
      const nameKey = compact(name);
      return nameKey && (askedKey === nameKey || nameKey.includes(askedKey));
    });
  }

  /**
   * 本轮该带的 rewrite_hint。
   *
   * 注意与 isSubjectOnly 的差别：后者允许「输入包含完整主体名」，
   * 于是「Brother HAK 180 烫金机怎么保养」也算纯主体名；此处若照抄，
   * 就会用上一轮反问的意图覆盖用户本轮自己写的「怎么保养」。故只认
   * 「相等」与「输入 ⊆ 候选」两种情形。
   */
  function resolveSendHint(isClick, clickedHint, text, pending) {
    if (isClick) return String(clickedHint || '').trim();
    if (pending && matchesPendingSubject(text, pending.names)) {
      return String(pending.sourceQuery || '').trim();
    }
    return '';
  }

  /** 渲染「没确认到主体」时的相似主体选项：点一下即以该主体重新提问。 */
  function renderItemNameOptions(msgEl, options, originalQuery) {
    const list = (Array.isArray(options) ? options : []).filter(o => o && o.item_name);
    if (!msgEl || !list.length || msgEl.querySelector('.option-bar')) return;
    // 记住本轮反问的来源，供「用户不点按钮、直接手输主体名」时补 rewrite_hint
    pendingSubjectAsk = {
      sourceQuery: String(originalQuery || '').trim(),
      names: list.map(o => String(o.item_name))
    };
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

  /**
   * 渲染「闲聊」徽标：意图识别为 chitchat 的回答与知识库作答在界面上区分。
   * 防抖：同一消息只渲染一次（final 与历史回显可能重复触发）。
   */
  function renderChitchatBadge(msgEl) {
    if (!msgEl || msgEl.querySelector('.chat-badge')) return;
    const bubble = msgEl.querySelector('.bubble');
    if (!bubble) return;
    const badge = App.create('span', 'chat-badge', '闲聊');
    const answerEl = bubble.querySelector('.answer');
    if (answerEl) bubble.insertBefore(badge, answerEl); // 答案上方
    else bubble.appendChild(badge);
  }

  /**
   * 订阅会话 SSE 并把事件渲染进回答气泡。
   *
   * 提交新问题与「刷新后重新接管」共用同一份逻辑，避免两处事件处理漂移。
   *
   * @param {Element} botMsgEl 回答气泡骨架
   * @param {string} sid 会话 ID（与后端 SSE 通道键一致）
   * @param {string} queryText 本轮提问原文（反馈回传用）
   */
  function attachStream(botMsgEl, sid, queryText) {
    const answerEl = ensureAnswerEl(botMsgEl);
    App.openStream(sid, {
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
          d && d.citations, d && d.groundedness, sessionId, queryText, d && d.item_names, d && d.intent, d && d.answer_id));
        renderItemNameOptions(botMsgEl, d && d.item_name_options, queryText);
        if (d && d.intent === 'chitchat') renderChitchatBadge(botMsgEl);
        if (d && d.issue_id) botMsgEl.querySelector('.bubble').appendChild(App.create('div', 'option-hint', '已登记待办：' + d.issue_id));
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
  }

  /**
   * @param {string} [rewriteHint] 点选主体时附带的意图原句（见 optionIntentHint）。
   *   只有点了主体选项才传；手动输入时为空，但若上一轮有挂起的反问、且本轮输入
   *   正是候选主体，则补上反问的来源问句（见 resolveSendHint），使手输与点选等价。
   *   非字符串入参（如 click 事件对象）会被丢弃。
   */
  async function onSend(rewriteHint) {
    if (sendBtn.disabled) return; // 按钮禁用时拦截重复提交（含 Enter 快捷键）
    const text = (inputEl.value || '').trim();
    if (!text) return;
    // 挂起的反问只服务一次发送：命中与否都作废，避免残留影响后续提问
    const pending = pendingSubjectAsk;
    pendingSubjectAsk = null;
    // 只接受字符串：非字符串（如 click 事件对象）一律丢弃，避免污染请求体
    const hint = resolveSendHint(typeof rewriteHint === 'string', rewriteHint, text, pending);
    inputEl.value = '';
    addUserMsg(text);
    const botMsgEl = addBotMsgSkeleton();
    sendBtn.disabled = true;

    try {
      const isStream = streamToggle.checked;
      const data = await submitQuery(text, hint);
      renderProgress(botMsgEl, [], [], 'pending');

      if (!isStream) {
        // 非流式：直接渲染完整结果
        renderProgress(botMsgEl, data.done_list || [], [], 'completed');
        finalizeBotAnswer(botMsgEl, data.answer, data.error, data.image_urls || [],
                          buildBotMeta(data.citations, data.groundedness, data.session_id, text,
                                       data.item_names, data.intent, data.answer_id));
        renderItemNameOptions(botMsgEl, data.item_name_options, text);
        if (data.intent === 'chitchat') renderChitchatBadge(botMsgEl);
        if (data.issue_id) botMsgEl.querySelector('.bubble').appendChild(App.create('div', 'option-hint', '已登记待办：' + data.issue_id));
        sendBtn.disabled = false;
        return;
      }

      // 流式：SSE 订阅与事件分发统一由公共库处理
      attachStream(botMsgEl, data.session_id, text);
    } catch (e) {
      const bubble = botMsgEl.querySelector('.bubble');
      const progress = botMsgEl.querySelector('details.progress');
      const typing = botMsgEl.querySelector('.typing');
      if (typing) typing.remove();
      bubble.innerHTML = `请求失败：${escapeHtml(e.message || e)}\n\n` + (progress ? progress.outerHTML : '');
      sendBtn.disabled = false;
    }
  }

  // 需要「重新接管」的任务状态；其余取值（completed / failed / 空串）一律以历史为准
  const ACTIVE_TASK_STATUS = { pending: true, processing: true };

  /** 读取进行中查询的进度快照；失败时按「无进行中查询」处理，不影响历史回显。 */
  async function fetchQueryStatus() {
    try {
      return await App.fetchJson(App.PATHS.queryStatus(sessionId));
    } catch (err) {
      console.warn('读取查询状态失败，按无进行中查询处理', err);
      return null;
    }
  }

  /**
   * 页面加载时恢复会话。
   *
   * 刷新会断开 SSE，但服务端查询仍在后台跑；此时历史里只有提问、没有回答，
   * 单靠 loadHistory 会让这一轮问答凭空消失。流程：
   *   1. 先读任务快照，判断是否还有查询在跑；
   *   2. 再读历史。顺序不可颠倒：反过来会出现「历史已含回答、快照仍说 processing」的窗口，
   *      于是重复渲染一个等待中的气泡；
   *   3. 快照说在跑、且历史最后一条不是回答 → 补出提问气泡（提问可能还没落库），
   *      用快照的 done/running 重建阶段进度，再重新订阅 SSE 等 final 收尾。
   */
  async function restoreSession() {
    const status = await fetchQueryStatus();
    const { lastRole, lastUserText, lastTurnId, lastUserTurnId } = await loadHistory();

    if (!status || !ACTIVE_TASK_STATUS[status.status]) return; // 无进行中查询：历史即全部
    if (lastRole === 'assistant' && status.turn_id && lastTurnId === status.turn_id) return; // 回答已落库：历史已完整，不再重复接管

    // 用户消息由主体确认节点落库，此刻可能还没写进去 → 用快照里的原文补上
    const hasCurrentUser = status.turn_id ? lastUserTurnId === status.turn_id : !!lastUserText;
    if (!hasCurrentUser && status.query) addUserMsg(status.query);

    const botMsgEl = addBotMsgSkeleton();
    renderProgress(botMsgEl, status.done_list, status.running_list, status.status);
    sendBtn.disabled = true;
    attachStream(botMsgEl, sessionId, status.query || lastUserText || '');
  }

  // 不能直接绑 onSend：浏览器会把 MouseEvent 当作第一个实参传进来，
  // 它会被误当成 rewrite_hint 发给服务端，触发参数校验失败。
  sendBtn.addEventListener('click', () => onSend());
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
    pendingSubjectAsk = null; // 选项栏已被清掉，挂起的反问随之作废
    scrollToBottom();
  });

  apiHealth();
  setInterval(apiHealth, 5000);
  // 不能只调 loadHistory：刷新时后台查询可能仍在跑，需先读任务快照再决定是否重新接管
  restoreSession();
  setTimeout(() => inputEl.focus(), 200);
})();
