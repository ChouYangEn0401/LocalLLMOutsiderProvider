/* Admin page: public URL + tunnel, config errors, backend health, every user's usage,
   all API keys (revoke), recent requests across users. */
(function () {
  'use strict';

  const { h, $, api, fill } = LL;
  let me = null;

  LL.boot(async () => {
    me = await LL.loadMe();
    if (!me.is_admin) {
      location.replace('/ui/dashboard');
      return;
    }
    LL.renderTopbar(me, 'admin');
    $('#refresh').addEventListener('click', () => load(true).catch((e) => LL.toast(e.message, true)));
    await load(false);
    setInterval(() => {
      if (document.visibilityState === 'visible') load(false).catch(() => {});
    }, 30000);
  });

  async function load(withToast) {
    const o = await api('GET', '/ui/api/admin/overview');
    renderAlerts(o);
    renderPublic(o);
    renderServer(o);
    renderBackends(o.backends || []);
    renderUsers(o.users || [], o.orphan_usage_month || []);
    renderKeys(o.keys || []);
    LL.limitedTable($('#recent'), o.recent || [], 20, (rows) => LL.requestsTable(rows, true));
    $('#updated').textContent = '更新於 ' + LL.fmtClock(o.server_time);
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

  function usageCell(used, limit) {
    if (!limit) {
      return h('div', { class: 'nowrap' }, h('span', { class: 'num' }, LL.fmtNum(used)), ' ',
        h('span', { class: 'pill' }, '不限'));
    }
    const m = LL.meter(used, limit, true);
    return h('div', { class: 'usage-cell' },
      h('div', { class: 'num small' }, LL.fmtNum(used), h('span', { class: 'muted' }, ' / ' + LL.fmtNum(limit))),
      m.el);
  }

  function ratio(used, limit) {
    return h('span', { class: 'num' }, LL.fmtNum(used),
      h('span', { class: 'muted' }, limit ? ' / ' + LL.fmtNum(limit) : ' / 不限'));
  }

  // ---------------------------------------------------------------- sections
  function renderAlerts(o) {
    const items = [];
    if (o.config_error) {
      items.push(h('div', { class: 'notice err', role: 'alert' },
        h('strong', null, '設定檔重新載入失敗 (config reload error)：'),
        '目前仍使用上一次正確的設定。錯誤：', h('code', null, o.config_error)));
    }
    const down = (o.backends || []).filter((b) => !b.reachable);
    if (down.length) {
      items.push(h('div', { class: 'notice warn' },
        h('strong', null, down.length + ' 個後端無法連線：'), down.map((b) => b.name).join('、')));
    }
    if (o.tunnel && o.tunnel.last_error) {
      items.push(h('div', { class: 'notice warn' }, h('strong', null, 'Tunnel 錯誤：'), String(o.tunnel.last_error)));
    }
    fill($('#alerts'), items.length ? h('div', { class: 'section' }, items) : null);
  }

  function renderPublic(o) {
    const t = o.tunnel || {};
    const parts = [h('h3', null, '公開網址 (public URL)')];
    if (o.public_url) {
      const input = h('input', { type: 'text', readonly: true, 'aria-label': '公開網址' });
      input.value = o.public_url;
      input.addEventListener('focus', () => input.select());
      parts.push(
        h('div', { class: 'copy-line' }, input, LL.copyButton(() => o.public_url),
          h('a', { class: 'btn small', href: o.public_url, target: '_blank', rel: 'noopener noreferrer' }, '開啟')),
        h('p', { class: 'small muted' },
          o.public_url_fixed ? '固定網址（server.public_url）' : 'Cloudflare tunnel 網址',
          o.public_url_updated_at ? h('span', null, ' · 更新於 ', LL.timeEl(o.public_url_updated_at)) : null,
          o.url_may_change ? ' · quick tunnel 重新啟動後網址會改變，使用者可在儀表板看到最新網址。' : ''));
    } else {
      parts.push(h('p', { class: 'notice warn' },
        '尚未取得公開網址。啟用 tunnel（tunnel.enabled: true，或 llmop serve --tunnel）或設定 server.public_url。'));
    }
    parts.push(h('h3', { class: 'section' }, 'Cloudflare tunnel'));
    parts.push(kv([
      ['設定檔', h('span', { class: 'mono small' }, 'enabled: ' + (t.enabled ? 'true' : 'false') + ' · mode: ' + (t.mode || ''))],
      ['程序', t.running ? h('span', { class: 'badge ok' }, '✓ 執行中') : h('span', { class: 'badge' }, '未執行 (not running)')],
      t.running ? ['狀態', stateBadge(t.status)] : null,
      t.url ? ['通道網址', h('span', { class: 'mono' }, t.url)] : null,
      t.restarts ? ['重啟次數', LL.fmtNum(t.restarts)] : null,
      t.started_at && typeof t.started_at === 'number' ? ['啟動', LL.timeEl(t.started_at)] : null,
      t.last_error ? ['最後錯誤', h('span', { class: 'small' }, String(t.last_error))] : null,
    ]));
    fill($('#public-card'), parts);
  }

  function renderServer(o) {
    fill($('#server-card'),
      h('h3', null, '服務 (server)'),
      kv([
        ['版本', 'llmop ' + (o.version || '')],
        ['啟動時間', o.started_at ? LL.timeEl(o.started_at) : '—'],
        ['設定檔', o.config_path ? h('span', { class: 'mono small' }, o.config_path) : '（記憶體中）'],
        ['設定狀態', o.config_error
          ? h('span', { class: 'badge err', title: o.config_error }, '✕ 重新載入失敗')
          : h('span', { class: 'badge ok' }, '✓ 正常')],
        ['帳號數', LL.fmtNum((o.users || []).length)],
      ]));
  }

  function renderBackends(rows) {
    fill($('#backends'), LL.table([
      { label: '名稱', cell: (b) => h('strong', null, b.name) },
      { label: '類型 (type)', cell: (b) => h('span', { class: 'badge' }, b.type || '—') },
      { label: '位址 (base_url)', cell: (b) => h('span', { class: 'mono small' }, b.base_url || '') },
      {
        label: '狀態', cell: (b) => b.reachable
          ? h('span', { class: 'badge ok' }, '✓ 正常')
          : h('span', { class: 'badge err' }, '✕ 無法連線'),
      },
      { label: '模型數', cls: 'num', cell: (b) => (b.model_count === null || b.model_count === undefined ? '—' : LL.fmtNum(b.model_count)) },
      { label: '錯誤', cell: (b) => (b.error ? h('span', { class: 'small sub' }, String(b.error)) : '') },
    ], rows, '沒有設定任何後端 (no backends configured)'));
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
      { label: '今日 Tokens', cell: (u) => usageCell(u.usage.tokens_today, u.usage.limits.tokens_per_day) },
      { label: '本月 Tokens', cell: (u) => usageCell(u.usage.tokens_month, u.usage.limits.tokens_per_month) },
      { label: '今日請求', cls: 'num', cell: (u) => ratio(u.usage.requests_today, u.usage.limits.requests_per_day) },
      { label: '每分鐘', cls: 'num', cell: (u) => ratio(u.usage.requests_last_minute, u.usage.limits.requests_per_minute) },
      { label: '進行中', cls: 'num', cell: (u) => ratio(u.usage.in_flight, u.usage.limits.max_concurrent) },
      { label: '金鑰', cls: 'num', cell: (u) => ratio(u.active_keys, u.max_api_keys) },
      {
        label: '', cls: 'right', cell: (u) => {
          const b = h('button', { type: 'button', class: 'btn small', title: '刪除這個帳號所有的登入 session' }, '強制登出');
          b.addEventListener('click', () => logoutUser(u, b));
          return b;
        },
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

  function renderKeys(keys) {
    $('#keys-count').textContent = '共 ' + keys.filter((k) => k.active).length + ' 把有效';
    fill($('#keys'), LL.table([
      { label: '帳號', cell: (k) => h('span', { class: 'mono' }, k.username) },
      { label: '名稱', cell: (k) => k.name || '—' },
      { label: '金鑰', cell: (k) => h('span', { class: 'key-display' }, k.display) },
      { label: '建立', cls: 'nowrap', cell: (k) => LL.timeEl(k.created_at) },
      { label: '最後使用', cls: 'nowrap', cell: (k) => LL.timeEl(k.last_used_at, '從未使用') },
      {
        label: '到期', cls: 'nowrap', cell: (k) => k.expired
          ? h('span', { class: 'badge err' }, '已過期')
          : LL.timeEl(k.expires_at, '永不過期'),
      },
      {
        label: '', cls: 'right', cell: (k) => {
          const b = h('button', { type: 'button', class: 'btn small danger' }, '撤銷');
          b.addEventListener('click', () => revokeKey(k, b));
          return b;
        },
      },
    ], keys, '還沒有任何 API 金鑰'));
  }

  // ---------------------------------------------------------------- actions
  async function revokeKey(k, btn) {
    if (!confirm('確定要撤銷 ' + k.username + ' 的金鑰「' + (k.name || k.display) + '」嗎？')) return;
    btn.disabled = true;
    try {
      await api('POST', '/ui/api/admin/revoke-key', { id: k.id });
      LL.toast('已撤銷金鑰');
      await load(false);
    } catch (e) {
      btn.disabled = false;
      LL.toast(e.message, true);
    }
  }

  async function logoutUser(u, btn) {
    const self = u.username === me.username;
    if (!confirm('確定要讓 ' + u.username + ' 在所有裝置登出嗎？' + (self ? '\n（這也會登出你自己）' : '') +
      '\n（API 金鑰不受影響；要停用帳號請在 config.yaml 設 disabled: true）')) return;
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
})();
