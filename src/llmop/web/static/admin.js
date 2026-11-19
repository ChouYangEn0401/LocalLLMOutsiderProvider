/* Admin page. On the local console (o.can_admin_write) it is the control panel:
   tunnels + "rotate API link", slots / queue / backends, accounts and tiers (bucket
   refill), GUEST keys, workers and enrolment tokens, all keys, raw config editing.
   On the public surface the same page is a masked, read-only view: the server has no
   write routes there, and no write control is drawn.
   Every server string goes through textContent (LL.h children); no HTML parsing. */
(function () {
  'use strict';

  const { h, $, api, fill } = LL;
  let me = null;
  let canWrite = false;
  let configSha = '';

  const TIER_NAMES = {
    vip3: 'VIP III', vip2: 'VIP II', vip1: 'VIP I', normal: 'NORMAL', guest: 'GUEST', low: 'LOW',
  };

  LL.boot(async () => {
    me = await LL.loadMe();
    if (!me.is_admin) {
      location.replace('/ui/dashboard');
      return;
    }
    canWrite = !!me.can_admin_write;
    LL.renderTopbar(me, 'admin');
    for (const el of document.querySelectorAll('.write-only')) el.classList.toggle('hidden', !canWrite);
    $('#readonly-note').classList.toggle('hidden', canWrite);
    $('#refresh').addEventListener('click', () => load(true).catch((e) => LL.toast(e.message, true)));
    if (canWrite) {
      bindGuestForm();
      bindWorkerForm();
    }
    await load(false);
    setInterval(() => {
      if (document.visibilityState === 'visible') load(false).catch(() => {});
    }, 30000);
  });

  async function load(withToast) {
    const o = await api('GET', '/ui/api/admin/overview');
    canWrite = !!o.can_admin_write;
    renderAlerts(o);
    renderLink(o);
    renderServer(o);
    renderQueue(o);
    renderBackends(o.backends || []);
    renderUsers(o.users || [], o.orphan_usage_month || []);
    renderGuestKeys(o.guest_keys || []);
    renderWorkers(o.workers || []);
    renderKeys(o.keys || []);
    LL.limitedTable($('#recent'), o.recent || [], 20, (rows) => LL.requestsTable(rows, true));
    $('#updated').textContent = '更新於 ' + LL.fmtClock(o.server_time);
    if (canWrite && o.config_editable && !configSha) {
      $('#config-section').classList.remove('hidden');
      bindConfig();
      await loadConfig();
    }
    if (withToast) LL.toast('已更新');
  }

  // ---------------------------------------------------------------- helpers
  function stateBadge(text) {
    const s = String(text || '');
    if (!s) return h('span', { class: 'muted' }, '—');
    let cls = 'badge';
    if (/(connected|running|ready|registered|ok|up)/i.test(s)) cls += ' ok';
    else if (/(error|fail|crash|dead|exit|stopped|down)/i.test(s)) cls += ' err';
    else cls += ' warn';
    return h('span', { class: cls }, s);
  }

  function kv(pairs) {
    return h('dl', { class: 'kv' }, pairs.filter(Boolean).map(([k, v]) => [h('dt', null, k), h('dd', null, v)]));
  }

  function ratio(used, limit) {
    return h('span', { class: 'num' }, LL.fmtNum(used),
      h('span', { class: 'muted' }, limit ? ' / ' + LL.fmtNum(limit) : ' / 不限'));
  }

  function button(label, cls, onClick) {
    const b = h('button', { type: 'button', class: 'btn small' + (cls ? ' ' + cls : '') }, label);
    b.addEventListener('click', () => onClick(b));
    return b;
  }

  async function act(btn, method, path, body, okText) {
    btn.disabled = true;
    try {
      const r = await api(method, path, body);
      if (okText) LL.toast(okText);
      await load(false);
      return r;
    } catch (e) {
      LL.toast(e.message, true);
      return null;
    } finally {
      btn.disabled = false;
    }
  }

  function secretBox(box, title, secret, note) {
    const input = h('input', { type: 'text', readonly: true, spellcheck: 'false', 'aria-label': title });
    input.value = secret;
    input.addEventListener('focus', () => input.select());
    const done = h('button', { type: 'button', class: 'btn' }, '我已複製，關閉');
    done.addEventListener('click', () => { input.value = ''; fill(box); });
    fill(box, h('div', { class: 'secret-box', role: 'alert' },
      h('h3', null, title), h('p', { class: 'small' }, h('strong', null, note)),
      h('div', { class: 'copy-line' }, input, LL.copyButton(() => secret)), done));
    input.focus();
  }

  function bucketCell(st) {
    if (!st) return h('span', { class: 'muted' }, '—');
    const b = st.bucket;
    if (!b) return h('span', { class: 'pill' }, '不限');
    const m = LL.meter(b.capacity - Math.max(0, b.level), b.capacity, true);
    return h('div', { class: 'usage-cell' },
      h('div', { class: 'num small' }, LL.fmtPct(b.percent), h('span', { class: 'muted' }, ' 剩餘')), m.el);
  }

  function weeklyCell(st) {
    if (!st) return h('span', { class: 'muted' }, '—');
    const w = st.weekly || st.guest;
    if (!w) return '—';
    const limit = w.limit || w.budget;
    if (!limit) return h('span', { class: 'nowrap' }, LL.fmtNum(w.used), ' ', h('span', { class: 'pill' }, '不限'));
    const m = LL.meter(w.used, limit, true);
    return h('div', { class: 'usage-cell' },
      h('div', { class: 'num small' }, LL.fmtNum(w.used), h('span', { class: 'muted' }, ' / ' + LL.fmtNum(limit))), m.el);
  }

  // ---------------------------------------------------------------- sections
  function renderAlerts(o) {
    const items = [];
    if (o.config_error) {
      items.push(h('div', { class: 'notice err', role: 'alert' },
        h('strong', null, '設定檔重新載入失敗：'), '目前仍使用上一次正確的設定。', h('code', null, String(o.config_error))));
    }
    const down = (o.backends || []).filter((b) => !b.reachable);
    if (down.length) {
      items.push(h('div', { class: 'notice warn' },
        h('strong', null, down.length + ' 個後端無法連線：'), down.map((b) => b.name).join('、')));
    }
    if (o.slots && o.slots.mismatch) {
      items.push(h('div', { class: 'notice warn' },
        h('strong', null, '名額與後端平行數不符：'),
        '全域名額 ' + o.slots.slots + '，後端合計只能同時處理 ' + o.slots.backend_capacity + ' 筆。'));
    }
    fill($('#alerts'), items.length ? h('div', { class: 'section' }, items) : null);
  }

  function tunnelRows(name, t) {
    t = t || {};
    return [
      [name, t.running ? stateBadge(t.status || 'running') : h('span', { class: 'badge' }, '未執行')],
      t.url ? [name + ' 網址', h('span', { class: 'mono small' }, String(t.url))] : null,
      t.last_error ? [name + ' 錯誤', h('span', { class: 'small' }, String(t.last_error))] : null,
      t.has_error ? [name + ' 錯誤', h('span', { class: 'badge err' }, '有錯誤')] : null,
    ];
  }

  function renderLink(o) {
    const link = o.api_link || {};
    const parts = [h('h3', null, 'API 連結')];
    if (link.url) {
      const input = h('input', { type: 'text', readonly: true, spellcheck: 'false', 'aria-label': 'API 連結' });
      input.value = link.url;
      parts.push(h('div', { class: 'copy-line' }, input, LL.copyButton(() => link.url)));
      if (link.updated_at) parts.push(h('p', { class: 'small muted' }, '更新於 ', LL.timeEl(link.updated_at)));
    } else {
      let note = link.available ? '目前沒有 API 連結。' : 'API 連結功能尚未啟用。';
      if (o.surface === 'public' && link.available) note = '唯讀檢視不顯示 API 連結；請看入口網頁。';
      parts.push(h('p', { class: 'muted' }, note));
    }
    if (canWrite && link.available) {
      const reason = h('input', { type: 'text', maxlength: 200, placeholder: '原因（選填）', 'aria-label': '更換原因' });
      parts.push(h('div', { class: 'row-form' },
        h('div', { class: 'field grow' }, reason),
        button('更換 API 連結（只換秘密路徑）', 'danger', (b) => rotate(b, false, reason.value)),
        button('連主機名一起換', 'danger', (b) => rotate(b, true, reason.value))));
      parts.push(h('p', { class: 'small muted' }, '舊連結會失效（除非設定寬限期）；API 金鑰不受影響，使用者在入口網頁看到新連結。'));
      parts.push(h('p', { class: 'small muted' }, '提醒：在別台電腦執行的 worker 是經由 API 連結連進來的；更換後要把它設定檔的 gateway_url 改成「新連結/worker/ws」（https 換成 wss）再重啟。經由舊連結連入的 worker 會在舊連結失效時被切斷。本機的 worker 不受影響。'));
    }
    const tn = o.tunnels || {};
    parts.push(h('h3', { class: 'section' }, '通道 (tunnels)'));
    parts.push(kv([...tunnelRows('帳號網站 #1', tn.ui), ...tunnelRows('API #2', tn.api)]));
    fill($('#link-card'), parts);
  }

  async function rotate(btn, changeHost, reason) {
    const what = changeHost ? '連同主機名一起更換（會重啟 API 通道）' : '更換秘密路徑';
    if (!confirm('確定要' + what + '？舊的 API 連結會失效。')) return;
    await act(btn, 'POST', '/ui/api/admin/api-link/rotate',
      { change_host: changeHost, reason: reason || '' },
      '已更換 API 連結。別台電腦的 worker 要改 gateway_url（新連結/worker/ws）。');
  }

  function renderServer(o) {
    fill($('#server-card'),
      h('h3', null, '服務 (server)'),
      kv([
        ['版本', 'llmop ' + (o.version || '')],
        ['入口', o.surface === 'public' ? '外部入口（唯讀）' : '本機控制台'],
        ['啟動時間', o.started_at ? LL.timeEl(o.started_at) : '—'],
        ['模型', o.model ? h('span', { class: 'mono' }, o.model) : h('span', { class: 'muted' }, '未設定')],
        o.config_path ? ['設定檔', h('span', { class: 'mono small' }, o.config_path)] : null,
        ['設定狀態', o.config_error
          ? h('span', { class: 'badge err' }, '✕ 重新載入失敗')
          : h('span', { class: 'badge ok' }, '✓ 正常')],
        ['帳號數', LL.fmtNum((o.users || []).length)],
      ]));
  }

  function renderQueue(o) {
    const q = o.queue || {};
    const s = o.slots || {};
    const rows = [
      ['全域名額', LL.fmtNum(s.slots || q.slots || 0) + (s.backend_capacity ? '（後端合計 ' + LL.fmtNum(s.backend_capacity) + '）' : '')],
    ];
    if (q.available) {
      rows.push(['執行中', LL.fmtNum(q.running)], ['排隊中', LL.fmtNum(q.waiting) + ' / 上限 ' + LL.fmtNum(q.max_length || 0)]);
      const byTier = q.by_tier || {};
      rows.push(['各等級', Object.keys(byTier).map((t) =>
        (TIER_NAMES[t] || t) + ' ' + byTier[t].running + ' 執行 / ' + byTier[t].waiting + ' 排隊').join('　')]);
    } else {
      rows.push(['佇列', h('span', { class: 'muted' }, '排程器未啟用')]);
    }
    fill($('#queue'), kv(rows));
  }

  function renderBackends(rows) {
    fill($('#backends'), LL.table([
      { label: '後端', cell: (b) => h('strong', null, b.name) },
      { label: '類型', cell: (b) => h('span', { class: 'badge' }, b.type || '—') },
      { label: '位址', cell: (b) => (b.base_url ? h('span', { class: 'mono small' }, b.base_url) : h('span', { class: 'muted' }, '—')) },
      {
        label: '狀態', cell: (b) => b.reachable
          ? h('span', { class: 'badge ok' }, '✓ 正常')
          : h('span', { class: 'badge err' }, '✕ 無法連線'),
      },
      { label: '錯誤', cell: (b) => (b.error ? h('span', { class: 'small sub' }, String(b.error)) : '') },
    ], rows, '沒有設定任何後端'));
  }

  function renderUsers(users, orphans) {
    fill($('#users'), LL.table([
      {
        label: '帳號', cell: (u) => h('div', null,
          h('strong', null, u.display_name), ' ',
          u.role === 'admin' ? h('span', { class: 'badge info' }, '管理員') : null, ' ',
          u.disabled ? h('span', { class: 'badge err' }, '停用') : null,
          h('div', { class: 'small muted mono' }, u.username)),
      },
      { label: '等級', cell: (u) => h('span', { class: 'badge' }, TIER_NAMES[u.tier] || String(u.tier || '')) },
      {
        label: '狀態', cell: (u) => (u.status && u.status.state === 'LOW'
          ? h('span', { class: 'badge warn' }, 'LOW') : h('span', { class: 'badge ok' }, 'NORMAL')),
      },
      { label: '1.5 小時', cell: (u) => bucketCell(u.status) },
      { label: '每週', cell: (u) => weeklyCell(u.status) },
      { label: '排隊', cls: 'num', cell: (u) => (u.queue ? u.queue.running + ' / ' + u.queue.waiting : '—') },
      { label: '金鑰', cls: 'num', cell: (u) => ratio(u.active_keys, u.max_api_keys) },
      {
        label: '', cls: 'right nowrap', cell: (u) => (canWrite ? [
          u.status && u.status.bucket ? button('補滿桶', '', (b) =>
            act(b, 'POST', '/ui/api/admin/refill', { username: u.username }, '已補滿 ' + u.username)) : null,
          ' ',
          button('強制登出', '', (b) => logoutUser(u, b)),
        ] : ''),
      },
    ], users, '沒有帳號'));
    const el = $('#orphans');
    if (orphans.length) {
      el.textContent = '本月還有已從設定檔移除的帳號用量：' +
        orphans.map((r) => r.username + '（' + LL.fmtNum(r.total) + ' tokens）').join('、');
      el.classList.remove('hidden');
    } else {
      el.classList.add('hidden');
    }
  }

  const GUEST_STATUS = {
    active: ['有效', 'ok'], exhausted: ['已用完', 'err'], used_up_until_reset: ['用完（等重置）', 'warn'],
    expired: ['已到期', 'err'], revoked: ['已撤銷', 'err'],
  };

  function renderGuestKeys(keys) {
    fill($('#guest-keys'), LL.table([
      { label: '標籤', cell: (k) => k.label || k.name || '—' },
      { label: '金鑰', cell: (k) => h('span', { class: 'key-display' }, k.display) },
      {
        label: '狀態', cell: (k) => {
          const s = (k.status && k.status.guest && k.status.guest.status) || (k.active ? 'active' : 'revoked');
          const [label, cls] = GUEST_STATUS[s] || [s, 'warn'];
          return h('span', { class: 'badge ' + cls }, label);
        },
      },
      { label: '加密', cell: (k) => (k.encryption === 'required' ? '一定加密' : '可不加密') },
      { label: '用量', cell: (k) => weeklyCell(k.status) },
      { label: '到期', cls: 'nowrap', cell: (k) => LL.timeEl(k.expires_at, '不到期') },
      {
        label: '', cls: 'right nowrap', cell: (k) => (canWrite && k.active ? [
          button('補滿桶', '', (b) => act(b, 'POST', '/ui/api/admin/refill', { key_id: k.id }, '已補滿')),
          ' ',
          button('撤銷', 'danger', (b) => revokeKey(k, b)),
        ] : ''),
      },
    ], keys, '還沒有 GUEST 金鑰'));
  }

  function renderWorkers(workers) {
    fill($('#workers'), LL.table([
      { label: '名稱', cell: (w) => h('div', null, h('strong', null, w.name), h('div', { class: 'small muted mono' }, w.id)) },
      {
        label: '狀態', cell: (w) => (w.revoked_at ? h('span', { class: 'badge err' }, '已撤銷')
          : w.online ? h('span', { class: 'badge ok' }, '線上') : h('span', { class: 'badge' }, '離線')),
      },
      { label: '模型', cell: (w) => (w.models ? w.models.join('、') : '—') },
      { label: '金鑰數', cls: 'num', cell: (w) => (w.keys ? LL.fmtNum(w.keys.length) : '—') },
      { label: '最後連線', cls: 'nowrap', cell: (w) => LL.timeEl(w.last_seen_at, '從未') },
      { label: '註冊碼到期', cls: 'nowrap', cell: (w) => LL.timeEl(w.reg_expires_at, '—') },
      {
        label: '', cls: 'right nowrap', cell: (w) => (canWrite && !w.revoked_at ? [
          button('新註冊碼', '', (b) => newRegToken(w, b)),
          ' ',
          button('撤銷', 'danger', (b) => {
            if (confirm('確定撤銷 worker「' + w.name + '」？它會立刻斷線。')) {
              act(b, 'POST', '/ui/api/admin/workers/' + encodeURIComponent(w.id) + '/revoke', {}, '已撤銷');
            }
          }),
        ] : ''),
      },
    ], workers, '還沒有 worker'));
  }

  function renderKeys(keys) {
    $('#keys-count').textContent = '共 ' + keys.filter((k) => k.active).length + ' 把有效';
    fill($('#keys'), LL.table([
      { label: '帳號', cell: (k) => h('span', { class: 'mono' }, k.username) },
      { label: '名稱', cell: (k) => k.name || '—' },
      { label: '金鑰', cell: (k) => h('span', { class: 'key-display' }, k.display) },
      { label: '加密', cell: (k) => (k.encryption === 'required' ? '一定加密' : '可不加密') },
      { label: '最後使用', cls: 'nowrap', cell: (k) => LL.timeEl(k.last_used_at, '從未使用') },
      {
        label: '到期', cls: 'nowrap', cell: (k) => (k.expired
          ? h('span', { class: 'badge err' }, '已過期') : LL.timeEl(k.expires_at, '永不過期')),
      },
      {
        label: '', cls: 'right', cell: (k) => (canWrite ? button('撤銷', 'danger', (b) => revokeKey(k, b)) : ''),
      },
    ], keys, '還沒有任何 API 金鑰'));
  }

  // ---------------------------------------------------------------- actions
  async function revokeKey(k, btn) {
    if (!confirm('確定要撤銷金鑰「' + (k.name || k.label || k.display) + '」嗎？')) return;
    await act(btn, 'POST', '/ui/api/admin/revoke-key', { id: k.id }, '已撤銷金鑰');
  }

  async function logoutUser(u, btn) {
    const self = u.username === me.username;
    if (!confirm('確定要讓 ' + u.username + ' 在所有裝置登出嗎？' + (self ? '\n（這也會登出你自己）' : ''))) return;
    btn.disabled = true;
    try {
      const r = await api('POST', '/ui/api/admin/logout-user', { username: u.username });
      if (self) {
        location.href = '/ui/login';
        return;
      }
      LL.toast('已登出 ' + r.sessions_deleted + ' 個 session');
    } catch (e) {
      LL.toast(e.message, true);
    }
    btn.disabled = false;
  }

  async function newRegToken(w, btn) {
    const r = await act(btn, 'POST', '/ui/api/admin/workers/' + encodeURIComponent(w.id) + '/reg-token', {}, null);
    if (r) secretBox($('#worker-new'), '「' + w.name + '」的新註冊碼', r.reg_token, '只顯示這一次，1 小時內有效（舊的註冊碼已失效）。');
  }

  function bindGuestForm() {
    const form = $('#guest-form');
    const err = $('#guest-error');
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      err.textContent = '';
      const body = {
        label: $('#guest-label').value.trim(),
        encryption: $('#guest-encryption').value,
        reset: $('#guest-reset').value === 'yes',
      };
      const budget = $('#guest-budget').value;
      if (budget) body.budget_tokens = Number(budget);
      const days = $('#guest-expires').value;
      if (days) body.expires_days = Number(days);
      const sig = $('#guest-sig').value.trim();
      if (sig) body.sig_public_key = sig;
      try {
        const r = await api('POST', '/ui/api/admin/guest-keys', body);
        form.reset();
        $('#guest-sig').value = '';
        secretBox($('#guest-new'), 'GUEST 金鑰「' + (r.record.label || r.record.display) + '」', r.key,
          '這是唯一一次顯示完整金鑰，請立即複製交給對方。');
        await load(false);
      } catch (e) {
        err.textContent = e.message;
      }
    });
  }

  function bindWorkerForm() {
    const form = $('#worker-form');
    const err = $('#worker-error');
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      err.textContent = '';
      try {
        const r = await api('POST', '/ui/api/admin/workers', { name: $('#worker-name').value.trim() });
        form.reset();
        secretBox($('#worker-new'), 'worker「' + r.worker.name + '」的註冊碼', r.reg_token,
          '只顯示這一次，1 小時內有效。在 worker 上執行 llmop worker init 時貼上。');
        await load(false);
      } catch (e) {
        err.textContent = e.message;
      }
    });
  }

  // ---------------------------------------------------------------- raw config
  let configBound = false;
  function bindConfig() {
    if (configBound) return;
    configBound = true;
    $('#config-reload').addEventListener('click', () => loadConfig().catch((e) => LL.toast(e.message, true)));
    $('#config-save').addEventListener('click', saveConfig);
  }

  async function loadConfig() {
    const r = await api('GET', '/ui/api/admin/config');
    $('#config-text').value = r.text;
    configSha = r.sha256;
    $('#config-error').textContent = '';
  }

  async function saveConfig() {
    const btn = $('#config-save');
    const err = $('#config-error');
    err.textContent = '';
    btn.disabled = true;
    try {
      const r = await api('PUT', '/ui/api/admin/config', { text: $('#config-text').value, base_sha256: configSha });
      configSha = r.sha256;
      LL.toast('設定檔已存檔並生效');
      await load(false);
    } catch (e) {
      err.textContent = e.message;
    }
    btn.disabled = false;
  }
})();
