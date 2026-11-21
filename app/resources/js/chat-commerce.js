(function () {
  'use strict';

  const RESOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
  const ACTIONS = new Map([
    ['shipping_reminder', { label: '催发货', field: 'note', fieldLabel: '备注（选填）' }],
    ['refund_application', { label: '申请退款', field: 'reason', fieldLabel: '退款原因' }]
  ]);
  const OPEN_CONFIRMATIONS = new Set(['checking', 'pending', 'submitting', 'unknown']);
  const ORDER_OPERATIONS = new Set(['order_detail', 'order_status', 'logistics', ...ACTIONS.keys()]);
  const TASK_LABELS = {
    order_detail: '查看订单详情',
    order_status: '查看订单状态',
    logistics: '查看物流',
    product_detail: '查看商品详情',
    shipping_reminder: '催发货',
    refund_application: '申请退款'
  };
  const TASK_HINTS = {
    missing_reason: '请在对话中输入退款原因。可以用“原因：”开头。',
    ready_to_preview: '请查看确认信息。',
    awaiting_confirmation: '请核对确认信息，或查询提交状态。'
  };

  function show(bubble, title, message) {
    bubble.replaceChildren();
    const panel = App.create('div', 'commerce-panel');
    panel.setAttribute('aria-live', 'polite');
    panel.append(App.create('strong', 'commerce-title', title));
    if (message) panel.append(App.create('p', 'commerce-note', message));
    bubble.append(panel);
    return panel;
  }

  function card(panel, title) {
    const section = App.create('section', 'commerce-card');
    if (title) section.append(App.create('h3', null, title));
    panel.append(section);
    return section;
  }

  function values(section, entries) {
    const list = App.create('dl', 'commerce-values');
    for (const [label, value] of entries) {
      if (value === '' || !['string', 'number', 'boolean'].includes(typeof value)) continue;
      list.append(App.create('dt', null, label), App.create('dd', null, String(value)));
    }
    if (list.children.length) section.append(list);
  }

  function money(value) {
    return typeof value === 'string' ? value : '';
  }

  function status(item) {
    return item.status_desc || item.status;
  }

  function cover(section, value) {
    if (typeof value !== 'string') return;
    let url;
    try { url = new URL(value); } catch (_) { return; }
    if (!['https:', 'http:'].includes(url.protocol) || !url.hostname || url.username || url.password) return;
    const image = App.create('img', 'commerce-cover');
    image.loading = 'lazy';
    image.alt = '';
    image.referrerPolicy = 'no-referrer';
    image.addEventListener('error', () => { image.hidden = true; });
    image.src = url.href;
    section.append(image);
  }

  function button(parent, label, act, primary = false) {
    const el = App.create('button', primary ? 'btn commerce-button' : 'btn plain commerce-button', label);
    el.type = 'button';
    el.addEventListener('click', async () => {
      if (el.disabled) return;
      el.disabled = true;
      try { await act(); } finally { el.disabled = false; }
    });
    parent.append(el);
    return el;
  }

  function orderDetails(section, order) {
    values(section, [
      ['订单编号', order.order_id], ['订单状态', status(order)], ['订单金额', money(order.amount)],
      ['下单时间', App.formatDateTime(order.created_at)]
    ]);
    if (!order.items?.length) return;
    const list = App.create('ul', 'commerce-items');
    for (const item of order.items) {
      const row = App.create('li');
      row.append(App.create('strong', null, item.title || item.product_id));
      values(row, [['数量', item.quantity], ['单价', money(item.price)]]);
      list.append(row);
    }
    section.append(list);
  }

  function productDetails(section, product) {
    cover(section, product.cover_url);
    values(section, [['商品编号', product.product_id], ['价格', money(product.price)], ['库存状态', product.stock_status]]);
    if (product.description) section.append(App.create('p', 'commerce-note', product.description));
    if (product.attributes) values(section, Object.entries(product.attributes));
  }

  function errorText(error) {
    let message = error.status ? error.message : '暂时无法连接商城服务。请重试。';
    if (error.status === 401) message = '请先在商城登录，然后重试。';
    if (error.status === 410) message = '确认已过期。请重新发起请求。';
    return error.code ? `${message} (${error.code})` : message;
  }

  function confirmationPath(id) {
    return '/confirmations/' + encodeURIComponent(id);
  }

  async function request(path, body) {
    const options = { token: '', cache: 'no-store', redirect: 'error' };
    if (body) Object.assign(options, { method: 'POST', body: JSON.stringify(body) });
    const result = await App.fetchJson('/api/commerce' + path, options);
    if (!result || typeof result !== 'object') throw new Error('无效的商城响应');
    return result;
  }

  window.createCommerceChat = function ({
    enabled, sessionId, workspace, addMessage, askKnowledge, scrollToBottom, onSelectionChange = () => {}
  }) {
    if (!enabled) return {
      assist: () => false, restore() {}, clearSelection() {}, clearConversation() {}, isBusy: () => false
    };

    const workspaceState = workspace?.querySelector('.commerce-workspace-state');
    const workspaceStatus = workspace?.querySelector('.commerce-workspace-status');
    const workspaceTasks = workspace?.querySelector('.commerce-workspace-tasks');
    const workspaceObjects = workspace?.querySelector('.commerce-workspace-objects');
    const reloadButton = workspace?.querySelector('.commerce-reload');
    const loginForm = workspace?.querySelector('.commerce-login-form');
    const accountInput = loginForm?.querySelector('.commerce-account-id');
    const passwordInput = loginForm?.querySelector('.commerce-account-password');
    const loginButton = loginForm?.querySelector('.commerce-login-submit');
    const logoutButton = workspace?.querySelector('.commerce-logout');
    const mobile = window.matchMedia?.('(max-width: 820px)');
    const privateMessages = new Set();
    const objectButtons = new Map();
    const confirmationStates = new Map();
    const confirmationViews = new Map();
    let account = '';
    let accountLoginEnabled = false;
    let accountPending = false;
    let accountRevision = 0;
    let workspaceLoad;
    let busy = false;
    let verifying = false;
    let selection = null;
    let objectTab = 'order_id';
    let active = null;
    let paused = null;
    let expectStaleTask = false;
    let taskRead = 0;

    function receiptKey() {
      return 'kb_commerce_confirmations:' + sessionId + ':' + account;
    }

    function saveReceipts() {
      if (!account) return;
      try {
        if (confirmationStates.size) {
          sessionStorage.setItem(receiptKey(), JSON.stringify([...confirmationStates.keys()]));
        } else {
          sessionStorage.removeItem(receiptKey());
        }
      } catch (_) { /* 存储受限时，当前页面仍可查询状态。 */ }
    }

    function remember(id, state) {
      confirmationStates.set(id, state);
      for (const [other, status] of confirmationStates) {
        if (other !== id && !OPEN_CONFIRMATIONS.has(status)) confirmationStates.delete(other);
      }
      saveReceipts();
    }

    function renderAccountControls() {
      if (loginForm) loginForm.hidden = !accountLoginEnabled || Boolean(account);
      if (loginButton) loginButton.disabled = accountPending;
      if (logoutButton) {
        logoutButton.hidden = !accountLoginEnabled || !account;
        logoutButton.disabled = accountPending;
      }
    }

    function setAccount(nextAccount) {
      if (account === nextAccount) return;
      account = nextAccount;
      accountRevision += 1;
      objectTab = 'order_id';
      expectStaleTask ||= Boolean(active);
      active = paused = null;
      for (const message of privateMessages) message.remove();
      privateMessages.clear();
      confirmationStates.clear();
      confirmationViews.clear();
      objectButtons.clear();
      workspaceTasks?.replaceChildren();
      workspaceObjects?.replaceChildren();
      setSelection(null);
      renderAccountControls();
      if (!account) return;
      try {
        for (const id of JSON.parse(sessionStorage.getItem(receiptKey()) || '[]')) {
          if (typeof id === 'string' && RESOURCE_ID.test(id)) confirmationStates.set(id, 'checking');
        }
      } catch (_) { /* 无法读取本地确认编号时，仍可恢复当前任务。 */ }
    }

    function setPrivateVisibility(visible) {
      verifying = !visible;
      for (const message of privateMessages) message.hidden = !visible;
      if (workspaceObjects) workspaceObjects.hidden = !visible;
      if (workspaceTasks) workspaceTasks.hidden = !visible;
      updateWorkspaceState();
    }

    function trackMessage(message) {
      if (!message) return;
      privateMessages.add(message);
      message.hidden = verifying;
    }

    function updateWorkspaceState() {
      onSelectionChange(verifying ? null : selection);
      if (!workspaceState) return;
      const items = [];
      if (!verifying) {
        if (active) items.push(TASK_LABELS[active.operation]);
        if (paused) items.push('有暂停的任务');
      }
      workspaceState.textContent = items.filter(Boolean).join(' · ');
    }

    function setSelection(nextSelection) {
      selection = nextSelection;
      for (const [control, resource] of objectButtons) {
        control.setAttribute('aria-pressed', resource.field === selection?.field && resource.id === selection.id);
      }
      updateWorkspaceState();
    }

    function renderWorkspaceObjects(data) {
      if (!Array.isArray(data.orders) || !Array.isArray(data.products)) throw new Error('无效的商城列表');
      workspaceObjects.replaceChildren();
      objectButtons.clear();
      const tabs = App.create('div', 'commerce-object-tabs');
      tabs.setAttribute('role', 'group');
      tabs.setAttribute('aria-label', '业务对象类型');
      workspaceObjects.append(tabs);
      const views = [];

      function selectTab(field) {
        objectTab = field;
        for (const view of views) {
          const selected = view.field === field;
          view.tab.setAttribute('aria-pressed', selected);
          view.section.hidden = !selected;
        }
      }

      for (const [label, title, field, objects, icon] of [
        ['订单', '最近订单', 'order_id', data.orders, 'order'],
        ['商品', '最近购买的商品', 'product_id', data.products, 'product']
      ]) {
        const section = App.create('section', 'commerce-object-group');
        section.id = 'commerce-objects-' + icon;
        section.setAttribute('aria-label', title);
        const tab = App.create('button', 'commerce-object-tab', label);
        tab.type = 'button';
        tab.setAttribute('aria-controls', section.id);
        tab.addEventListener('click', () => selectTab(field));
        tabs.append(tab);
        views.push({ field, tab, section });
        workspaceObjects.append(section);
        if (!objects.length) {
          section.append(App.create('p', 'commerce-object-empty', `暂无${label}。`));
          continue;
        }
        const list = App.create('ul', 'commerce-object-list');
        section.append(list);
        for (const item of objects) {
          const id = item[field];
          if (typeof id !== 'string' || !RESOURCE_ID.test(id)) continue;
          const row = App.create('li');
          const control = App.create('button', 'commerce-object');
          control.type = 'button';
          const thumb = App.create('span', 'shortcut-icon commerce-object-thumb');
          thumb.dataset.icon = icon;
          thumb.setAttribute('aria-hidden', 'true');
          cover(thumb, item.cover_url);
          const text = App.create('span', 'commerce-object-text');
          const title = item.title || id;
          text.append(App.create('strong', null, title), App.create('span', 'commerce-object-id', id));
          const detail = App.create('span', 'commerce-object-meta');
          if (field === 'order_id' && status(item)) detail.append(App.create('span', 'commerce-object-status', status(item)));
          const price = money(field === 'order_id' ? item.amount : item.price);
          if (price) detail.append(App.create('span', 'commerce-object-price', price));
          text.append(detail);
          control.append(thumb, text);
          const resource = { field, id, title };
          objectButtons.set(control, resource);
          control.addEventListener('click', () => {
            setSelection(selection === resource ? null : resource);
            if (selection && mobile?.matches) {
              workspace.open = false;
              workspace.querySelector('summary').focus();
            }
          });
          row.append(control);
          list.append(row);
        }
      }
      selectTab(objectTab);
      const available = [...objectButtons.values()].find(item => item.field === selection?.field && item.id === selection.id);
      setSelection(available || null);
    }

    function newBusinessBubble() {
      const message = addMessage();
      trackMessage(message);
      return message.querySelector('.bubble');
    }

    function renderTask(task, isPaused) {
      const section = card(workspaceTasks);
      section.className += ' commerce-task';
      const label = TASK_LABELS[task.operation] || '商城请求';
      section.append(App.create('h2', null, (isPaused ? '已暂停 · ' : '当前任务 · ') + label));
      const id = task.order_id || task.product_id;
      if (id) section.append(App.create('p', 'commerce-note', id));
      if (!isPaused) {
        const hint = task.stage === 'missing_target'
          ? (task.operation === 'product_detail' ? '请在对话中输入商品编号。' : '请在对话中输入订单编号。')
          : TASK_HINTS[task.stage];
        if (hint) section.append(App.create('p', 'commerce-note', hint));
      }
      const actions = App.create('div', 'commerce-workspace-actions');
      section.append(actions);
      const run = query => () => {
        if (!busy && !verifying) return routeAssist(newBusinessBubble(), { query });
      };
      if (isPaused) {
        button(actions, '继续之前的任务', run('继续之前的任务'), true);
        button(actions, '取消之前的任务', run('取消之前的任务'));
      } else {
        button(actions, '打开当前任务', run('继续当前任务'), true);
        if (!paused) button(actions, '暂停当前任务', run('暂停当前任务'));
        button(actions, '取消当前任务', run('取消当前任务'));
      }
    }

    function renderTasks() {
      if (workspaceTasks) {
        workspaceTasks.replaceChildren();
        if (active) renderTask(active, false);
        if (paused) renderTask(paused, true);
      }
      for (const [id, view] of confirmationViews) {
        const isPaused = paused?.confirmation_id === id;
        for (const control of view.controls) control.hidden = isPaused;
        if (view.pausedNote) view.pausedNote.hidden = !isPaused;
      }
      updateWorkspaceState();
    }

    function handleAccountError(error) {
      if (error.status !== 401 && error.code !== 'session_not_found') return;
      setAccount('');
      if (workspaceStatus) workspaceStatus.textContent = errorText(error);
    }

    async function readTask(background = false) {
      if (!workspace || !account) return;
      const revision = accountRevision;
      const read = ++taskRead;
      try {
        const data = await request('/task?session_id=' + encodeURIComponent(sessionId));
        if (revision !== accountRevision || read !== taskRead) return;
        const priorIds = [active?.confirmation_id, paused?.confirmation_id];
        if (background && active && !data.active) expectStaleTask = true;
        active = data.active;
        paused = data.paused;
        renderTasks();
        for (const id of priorIds) {
          const view = confirmationViews.get(id);
          if (!view || !OPEN_CONFIRMATIONS.has(confirmationStates.get(id))
              || id === active?.confirmation_id || id === paused?.confirmation_id) continue;
          for (const control of view.controls) control.hidden = true;
          try {
            const confirmation = await request(confirmationPath(id));
            if (revision !== accountRevision || read !== taskRead) return;
            renderConfirmation(view.bubble, id, confirmation);
          } catch (error) {
            if (revision !== accountRevision || read !== taskRead) return;
            renderConfirmationError(view.bubble, id, error);
          }
        }
      } catch (error) {
        if (revision !== accountRevision || read !== taskRead) return;
        handleAccountError(error);
        if (account) {
          renderTasks();
          workspaceTasks.append(App.create('p', 'commerce-note', '暂时无法读取任务。请刷新。'));
        }
      }
    }

    function showWorkspaceError(error) {
      handleAccountError(error);
      setSelection(null);
      objectButtons.clear();
      workspaceObjects.replaceChildren();
      workspaceStatus.textContent = errorText(error);
    }

    async function loadWorkspace() {
      reloadButton.disabled = true;
      workspaceStatus.textContent = '正在读取商城。';
      setPrivateVisibility(false);
      let revision = accountRevision;
      let verified = false;
      try {
        const identity = await request('/session?include_scope=true');
        if (revision !== accountRevision) return;
        if (identity.authenticated && typeof identity.account_scope !== 'string') throw new Error('无法核验商城账号');
        accountLoginEnabled = identity.account_login_enabled === true;
        setAccount(identity.authenticated ? identity.account_scope : '');
        renderAccountControls();
        revision = accountRevision;
        verified = true;
        if (!account) {
          workspaceStatus.textContent = accountLoginEnabled ? '' : '请先在商城登录，再点击刷新。';
          return;
        }
        const [objects] = await Promise.allSettled([request('/objects'), readTask(true)]);
        if (revision !== accountRevision) return;
        if (objects.status === 'rejected') showWorkspaceError(objects.reason);
        else {
          renderWorkspaceObjects(objects.value);
          workspaceStatus.textContent = '';
        }
        const ids = new Set(confirmationStates.keys());
        if (active?.confirmation_id) ids.add(active.confirmation_id);
        ids.delete(paused?.confirmation_id);
        for (const id of ids) {
          if (revision !== accountRevision || busy) break;
          if (!confirmationViews.has(id)) await updateConfirmation(newBusinessBubble(), id);
        }
      } catch (error) {
        if (revision === accountRevision) showWorkspaceError(error);
      } finally {
        reloadButton.disabled = false;
        if (verified && !document.hidden) setPrivateVisibility(true);
      }
    }

    function refreshWorkspace() {
      if (!workspace) return;
      if (!workspaceLoad) workspaceLoad = loadWorkspace().finally(() => { workspaceLoad = null; });
      return workspaceLoad;
    }

    async function changeAccount(path, body) {
      if (!accountLoginEnabled || accountPending || busy) return;
      accountPending = true;
      renderAccountControls();
      try {
        if (workspaceLoad) await workspaceLoad;
        setPrivateVisibility(false);
        await request(path, body);
        setAccount('');
        await refreshWorkspace();
      } catch (error) {
        workspaceStatus.textContent = error.code === 'commerce_login_failed' ? error.message : errorText(error);
        if (!document.hidden) setPrivateVisibility(true);
      } finally {
        accountPending = false;
        renderAccountControls();
      }
    }

    function renderResult(bubble, operation, result) {
      const lookup = (action, ids) => routeAssist(bubble, { operation: action, ...ids });
      switch (operation) {
        case 'order_list': {
          const panel = show(bubble, '最近订单', result.orders.length ? '' : '没有找到订单。');
          for (const order of result.orders) {
            const section = card(panel, order.title || order.order_id);
            cover(section, order.cover_url);
            orderDetails(section, order);
            button(section, '查看订单', () => lookup('order_detail', { order_id: order.order_id }));
          }
          break;
        }
        case 'recent_products': {
          const panel = show(bubble, '最近商品', result.products.length ? '' : '没有找到商品。');
          for (const product of result.products) {
            const section = card(panel, product.title);
            productDetails(section, product);
            button(section, '查看商品', () => lookup('product_detail', { product_id: product.product_id }));
          }
          break;
        }
        case 'product_detail':
          productDetails(card(show(bubble, '商品详情'), result.title), result);
          break;
        case 'order_detail':
        case 'order_status': {
          const section = card(show(bubble, operation === 'order_detail' ? '订单详情' : '订单状态'));
          orderDetails(section, result);
          button(section, '查看物流', () => lookup('logistics', { order_id: result.order_id }));
          break;
        }
        case 'logistics': {
          const section = card(show(bubble, '物流信息'));
          values(section, [
            ['订单编号', result.order_id], ['物流公司', result.logistics_company],
            ['运单编号', result.tracking_number], ['物流状态', status(result)]
          ]);
          if (result.traces?.length) {
            const list = App.create('ol', 'commerce-items');
            for (const trace of result.traces) {
              const row = App.create('li', null, trace.desc);
              row.append(App.create('div', 'commerce-note', App.formatDateTime(trace.time)));
              list.append(row);
            }
            section.append(list);
          } else {
            section.append(App.create('p', 'commerce-note', '暂无物流记录。'));
          }
          break;
        }
        default:
          throw new Error('无效的商城操作');
      }
    }

    function selectedTarget(operation) {
      if (!selection || verifying) return {};
      const expected = operation === 'product_detail' ? 'product_id'
        : ORDER_OPERATIONS.has(operation) ? 'order_id' : '';
      return selection.field === expected ? { [selection.field]: selection.id } : {};
    }

    function renderChoices(bubble, data, useSelection) {
      const policy = data.type === 'policy_choice';
      const product = data.operation === 'product_detail';
      const title = policy ? '请选择要办理的事项' : product ? '请选择商品' : '请选择订单';
      const panel = show(bubble, title, data.message);
      for (const choice of data.choices) {
        const section = card(panel);
        const label = policy || choice.label === choice.id ? choice.label : `${choice.label} (${choice.id})`;
        button(section, label, async () => {
          if (policy && choice.id === 'knowledge') {
            if (askKnowledge(data.query)) show(bubble, '已选择查看政策');
            return;
          }
          const target = policy
            ? ((data.order_id || data.product_id) ? { order_id: data.order_id, product_id: data.product_id }
              : useSelection && data.accepts_selection !== false ? selectedTarget(data.operation) : {})
            : product ? { product_id: choice.id } : { order_id: choice.id };
          await routeAssist(bubble, { operation: data.operation, ...target });
        });
      }
      if (!policy) {
        const target = product ? '商品' : '订单';
        const form = App.create('form', 'commerce-target-form');
        const label = App.create('label', 'commerce-field', `${target}编号`);
        const input = App.create('input');
        input.type = 'text';
        input.required = true;
        input.autocomplete = 'off';
        input.maxLength = 64;
        label.append(input);
        const submit = App.create('button', 'btn plain commerce-button', `使用此${target}编号`);
        submit.type = 'submit';
        const note = App.create('div', 'commerce-note');
        form.append(label, note, submit);
        form.addEventListener('submit', async event => {
          event.preventDefault();
          const id = input.value.trim();
          if (!RESOURCE_ID.test(id)) {
            note.textContent = `请输入有效的${target}编号。`;
            input.focus();
            return;
          }
          const idField = product ? 'product_id' : 'order_id';
          await routeAssist(bubble, { operation: data.operation, [idField]: id });
        });
        panel.append(form);
      }
    }

    function renderOperation(bubble, data) {
      const action = ACTIONS.get(data.operation);
      if (!action) throw new Error('无效的商城操作');
      const panel = show(bubble, action.label);
      orderDetails(card(panel), data.summary);
      const field = App.create('label', 'commerce-field', action.fieldLabel);
      const input = App.create('textarea');
      input.rows = 2;
      input.maxLength = 255;
      field.append(input);
      panel.append(field);
      const notice = App.create('p', 'commerce-note');
      panel.append(notice);
      button(panel, '查看确认信息', async () => {
        if (busy || verifying) return;
        if (active?.operation === data.operation && active.order_id === data.order_id && active.confirmation_id) {
          await updateConfirmation(bubble, active.confirmation_id);
          return;
        }
        const text = input.value.trim();
        if (!text && action.field === 'reason') {
          notice.textContent = '请输入退款原因。';
          input.focus();
          return;
        }
        busy = true;
        const revision = accountRevision;
        notice.textContent = '正在读取确认信息。';
        try {
          const body = { operation: data.operation, order_id: data.order_id, session_id: sessionId };
          if (text) body[action.field] = text;
          const confirmation = await request('/confirmations', body);
          if (revision !== accountRevision) return;
          if (!confirmation.confirmation_id) throw new Error('缺少确认编号');
          renderConfirmation(bubble, confirmation.confirmation_id, confirmation);
        } catch (error) {
          if (revision !== accountRevision) return;
          handleAccountError(error);
          notice.textContent = errorText(error);
        } finally {
          if (revision === accountRevision) await readTask();
          busy = false;
          scrollToBottom();
        }
      });
    }

    function renderConfirmation(bubble, id, data) {
      const action = ACTIONS.get(data.operation);
      if (!action || data.confirmation_id !== id) throw new Error('无效的确认信息');
      remember(id, data.status);
      const previous = confirmationViews.get(id)?.bubble;
      if (previous && previous !== bubble) show(previous, '已打开此确认');
      const view = { bubble, controls: [] };
      confirmationViews.set(id, view);
      if (data.status === 'succeeded' && data.result) {
        const title = data.operation === 'refund_application' ? '退款申请已提交' : '催发货已提交';
        const section = card(show(bubble, title));
        values(section, [
          ['订单编号', data.result.order_id], ['申请编号', data.result.request_id], ['状态', status(data.result)]
        ]);
        return;
      }
      if (data.status === 'cancelled') {
        show(bubble, '已取消确认');
        return;
      }
      const messages = {
        pending: '请核对信息后提交。',
        submitting: '提交正在处理。请查询提交状态。',
        unknown: '暂时无法确认提交结果。',
        failed: '提交失败。',
        expired: '确认已过期。请重新发起请求。'
      };
      const panel = show(bubble, action.label, messages[data.status] || '请查询提交状态。');
      if (data.status === 'failed' && data.error?.message) {
        panel.append(App.create('p', 'commerce-note', `${data.error.message} (${data.error.code})`));
      }
      if (data.preview) {
        const section = card(panel);
        orderDetails(section, data.preview);
        values(section, [[action.field === 'reason' ? '退款原因' : '备注', data.preview[action.field]]]);
      }
      if (data.status === 'pending') {
        values(panel, [['确认有效期', App.formatDateTime(data.expires_at)]]);
        if (data.retryable === true) {
          const label = data.operation === 'refund_application' ? '提交退款申请' : '提交催发货';
          view.controls.push(button(panel, label, () => updateConfirmation(bubble, id, 'submit'), true));
        }
        view.controls.push(button(panel, '取消确认', () => updateConfirmation(bubble, id, 'cancel')));
      }
      if (data.status === 'unknown' || data.status === 'submitting') {
        values(panel, [['重试期限', App.formatDateTime(data.retry_until)]]);
        if (data.status === 'unknown' && data.retryable === true) {
          view.controls.push(button(panel, '重试提交', () => updateConfirmation(bubble, id, 'submit'), true));
        }
        button(panel, '查询提交状态', () => updateConfirmation(bubble, id));
      }
      if (view.controls.length) {
        view.pausedNote = App.create('p', 'commerce-note', '此任务已暂停。请先继续任务。');
        const isPaused = paused?.confirmation_id === id;
        view.pausedNote.hidden = !isPaused;
        for (const control of view.controls) control.hidden = isPaused;
        panel.append(view.pausedNote);
      }
    }

    async function updateConfirmation(bubble, id, action) {
      if (busy || (action && (verifying || paused?.confirmation_id === id))) return;
      const state = confirmationStates.get(id);
      if (action === 'submit' && state !== 'pending' && state !== 'unknown') return;
      if (action === 'cancel' && state !== 'pending') return;
      const revision = accountRevision;
      busy = true;
      remember(id, 'checking');
      const progress = action === 'submit' ? '正在提交' : action === 'cancel' ? '正在取消确认' : '正在查询确认状态';
      show(bubble, progress);
      try {
        const path = confirmationPath(id);
        const data = action ? await request(path + '/' + action, {}) : await request(path);
        if (revision === accountRevision) renderConfirmation(bubble, id, data);
      } catch (error) {
        if (revision === accountRevision) renderConfirmationError(bubble, id, error, action);
      } finally {
        if (!verifying && revision === accountRevision) await readTask();
        busy = false;
        scrollToBottom();
      }
    }

    function renderConfirmationError(bubble, id, error, action) {
      handleAccountError(error);
      if (error.code === 'confirmation_cancelled') {
        remember(id, 'cancelled');
        show(bubble, '已取消确认');
        return;
      }
      const missing = !action && error.status === 404;
      const expired = error.status === 410;
      if (missing || expired) {
        confirmationStates.delete(id);
        saveReceipts();
      }
      const message = action === 'submit' && !error.status ? '暂时无法确认提交结果。请查询提交状态。' : errorText(error);
      const panel = show(bubble, '商城确认状态', message);
      if (!missing && !expired) button(panel, '查询提交状态', () => updateConfirmation(bubble, id));
    }

    async function routeAssist(bubble, payload, useSelection = false) {
      if (busy) {
        show(bubble, '商城服务', '请等待当前商城请求完成。');
        return true;
      }
      busy = true;
      const revision = accountRevision;
      if (payload.operation) show(bubble, '正在查询商城');
      try {
        let data = await request('/assist', { ...payload, session_id: sessionId });
        if (revision !== accountRevision) return true;
        expectStaleTask = false;
        if (data.type === 'knowledge' && payload.query) return false;
        if (data.type === 'choose' && useSelection && data.accepts_selection !== false) {
          const target = selectedTarget(data.operation);
          if (Object.keys(target).length) {
            data = await request('/assist', { operation: data.operation, ...target, session_id: sessionId });
            if (revision !== accountRevision) return true;
          }
        }
        if (data.type === 'result') renderResult(bubble, data.operation, data.result);
        else if (data.type === 'choose' || data.type === 'policy_choice') renderChoices(bubble, data, useSelection);
        else if (data.type === 'operation') renderOperation(bubble, data);
        else if (data.type === 'confirmation') renderConfirmation(bubble, data.confirmation.confirmation_id, data.confirmation);
        else if (data.type === 'task_control') show(bubble, '商城任务', data.message);
        else if (data.type === 'unsupported') show(bubble, '商城服务', data.message || '商城暂时无法办理此请求。');
        else throw new Error('无效的商城响应');
        await readTask();
      } catch (error) {
        if (revision !== accountRevision) return true;
        handleAccountError(error);
        const panel = show(bubble, '商城请求失败', errorText(error));
        if (!error.status || error.status === 401 || error.status >= 500) {
          button(panel, '重新查询', async () => {
            if (!await routeAssist(bubble, payload, useSelection) && askKnowledge(payload.query)) show(bubble, '已转到知识问答');
          });
        }
        if (revision === accountRevision) await readTask();
      } finally {
        busy = false;
        scrollToBottom();
      }
      return true;
    }

    if (workspace) {
      if (mobile?.matches) workspace.open = false;
      mobile?.addEventListener?.('change', event => { workspace.open = !event.matches; });
      workspace.hidden = false;
      reloadButton.addEventListener('click', () => { if (!accountPending) return refreshWorkspace(); });
      loginForm?.addEventListener('submit', event => {
        event.preventDefault();
        const credentials = { user_id: accountInput.value.trim(), password: passwordInput.value };
        passwordInput.value = '';
        return changeAccount('/login', credentials);
      });
      logoutButton?.addEventListener('click', () => changeAccount('/logout', {}));
      let inactive = Boolean(document.hidden);
      const reactivate = () => {
        if (!inactive || document.hidden || accountPending) return;
        inactive = false;
        return refreshWorkspace();
      };
      window.addEventListener('blur', () => { inactive = true; });
      window.addEventListener('focus', reactivate);
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) return reactivate();
        inactive = true;
        setPrivateVisibility(false);
      });
    }

    return {
      async assist(message, query, userMessage) {
        if (workspaceLoad) await workspaceLoad;
        const revision = accountRevision;
        const payload = { query };
        if (active || expectStaleTask) payload.expect_task = true;
        const handled = await routeAssist(message.querySelector('.bubble'), payload, true);
        if (handled) {
          if (revision !== accountRevision) userMessage?.remove();
          else trackMessage(userMessage);
          trackMessage(message);
        }
        return handled;
      },
      restore: refreshWorkspace,
      clearSelection: () => setSelection(null),
      clearConversation() {
        privateMessages.clear();
        confirmationViews.clear();
        setSelection(null);
      },
      isBusy: () => busy || accountPending
    };
  };
})();
