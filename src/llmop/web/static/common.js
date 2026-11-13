/* llmop web UI — shared helpers (window.LL). Vanilla JS, no dependencies.
   CSP forbids inline script/style: build DOM with LL.h()/textContent only, and set
   sizes through the CSSOM (el.style.width = ...), never a style="" attribute. */
(function () {
  'use strict';

  const LL = (window.LL = {});
  LL.csrf = '';
  LL.me = null;

  // ------------------------------------------------------------------ DOM
  LL.$ = (sel, root) => (root || document).querySelector(sel);

  function append(el, children) {
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function setAttrs(el, attrs) {
    if (!attrs) return el;
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.setAttribute('class', v);
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    return el;
  }

  LL.h = (tag, attrs, ...children) => append(setAttrs(document.createElement(tag), attrs), children);
  LL.svg = (tag, attrs, ...children) =>
    append(setAttrs(document.createElementNS('http://www.w3.org/2000/svg', tag), attrs), children);
  LL.fill = (el, ...children) => { el.replaceChildren(); return append(el, children); };

  // ------------------------------------------------------------------ formatting
  const nf = new Intl.NumberFormat('zh-TW');
  const cf = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
  const rtf = new Intl.RelativeTimeFormat('zh-TW', { numeric: 'auto' });

  LL.fmtNum = (n) => nf.format(Math.round(Number(n) || 0));
  LL.fmtCompact = (n) => cf.format(Number(n) || 0);
  LL.pct = (part, whole) => (whole > 0 ? (100 * part) / whole : 0);
  LL.fmtPct = (p) => (p > 0 && p < 1 ? '<1%' : Math.round(p) + '%');

  LL.fmtDateTime = (ts) => new Date(ts * 1000).toLocaleString('zh-TW', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  LL.fmtShortDateTime = (ts) => new Date(ts * 1000).toLocaleString('zh-TW', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  });
  LL.fmtClock = (ts) => new Date(ts * 1000).toLocaleTimeString('zh-TW', {
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
  LL.fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString('zh-TW', { month: 'numeric', day: 'numeric' });

  LL.fmtRel = (ts) => {
    const diff = ts * 1000 - Date.now();
    const s = Math.round(diff / 1000);
    const a = Math.abs(s);
    if (a < 45) return s <= 0 ? '剛剛' : '即將';
    if (a < 3600) return rtf.format(Math.round(s / 60), 'minute');
    if (a < 86400) return rtf.format(Math.round(s / 3600), 'hour');
    if (a < 86400 * 30) return rtf.format(Math.round(s / 86400), 'day');
    return new Date(ts * 1000).toLocaleDateString('zh-TW');
  };

  LL.fmtDuration = (ms) => {
    ms = Number(ms) || 0;
    if (ms < 1000) return ms + ' ms';
    if (ms < 60000) return (ms / 1000).toFixed(1) + ' 秒';
    return Math.floor(ms / 60000) + ' 分 ' + Math.round((ms % 60000) / 1000) + ' 秒';
  };

  /** <time> with a relative label and the full timestamp on hover. */
  LL.timeEl = (ts, empty) => {
    if (!ts) return LL.h('span', { class: 'muted' }, empty || '—');
    return LL.h('time', { datetime: new Date(ts * 1000).toISOString(), title: LL.fmtDateTime(ts) },
      LL.fmtRel(ts));
  };

  // ------------------------------------------------------------------ API
  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.status = status;
      this.data = data;
    }
  }
  LL.ApiError = ApiError;

  LL.api = async function (method, path, body) {
    const headers = { Accept: 'application/json' };
    const opts = { method, headers, credentials: 'same-origin', cache: 'no-store' };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    if (method !== 'GET' && LL.csrf) headers['X-CSRF-Token'] = LL.csrf;
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new ApiError('無法連線到伺服器，請檢查網路 (network error).', 0, null);
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (res.status === 401 && path !== '/ui/api/login') {
      LL.goLogin();
      throw new ApiError('登入已過期，請重新登入 (session expired).', 401, data);
    }
    if (!res.ok) {
      const err = data && data.error;
      const msg = (err && (err.message || (typeof err === 'string' ? err : ''))) ||
        (data && data.detail) || ('HTTP ' + res.status);
      throw new ApiError(String(msg), res.status, data);
    }
    return data;
  };

  LL.goLogin = () => {
    const next = location.pathname;
    location.href = '/ui/login' + (/^\/ui\/[a-z]+$/.test(next) ? '?next=' + encodeURIComponent(next) : '');
  };

  LL.loadMe = async () => {
    const me = await LL.api('GET', '/ui/api/me');
    LL.me = me;
    LL.csrf = me.csrf;
    return me;
  };

  LL.logout = async () => {
    try { await LL.api('POST', '/ui/api/logout'); } catch (e) { /* ignore */ }
    location.href = '/ui/login';
  };

  // ------------------------------------------------------------------ UI bits
  let toastTimer = null;
  LL.toast = (msg, isError) => {
    let el = document.getElementById('toast');
    if (!el) {
      el = LL.h('div', { id: 'toast', class: 'toast hidden', role: 'status', 'aria-live': 'polite' });
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.className = 'toast' + (isError ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add('hidden'), isError ? 5000 : 2500);
  };

  LL.copy = async (text) => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) { /* fall through */ }
    const ta = LL.h('textarea', { class: 'sr-only', readonly: true });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  };

  LL.copyButton = (getText, label) => {
    label = label || '複製';
    const btn = LL.h('button', { type: 'button', class: 'btn small copy' }, label);
    btn.addEventListener('click', async () => {
      const ok = await LL.copy(typeof getText === 'function' ? getText() : getText);
      btn.textContent = ok ? '已複製' : '請手動複製';
      setTimeout(() => { btn.textContent = label; }, 1600);
    });
    return btn;
  };

  LL.codeBlock = (text) => LL.h('div', { class: 'code-block' },
    LL.copyButton(text), LL.h('pre', { class: 'code' }, text));

  /** Progress meter; level turns warn at 70%, critical at 90%. */
  LL.meter = (used, limit, thin) => {
    const p = limit > 0 ? Math.min(100, LL.pct(used, limit)) : 0;
    const level = limit > 0 ? (p >= 90 ? 'crit' : p >= 70 ? 'warn' : '') : '';
    const fill = LL.h('div', { class: 'meter-fill' });
    fill.style.width = p.toFixed(1) + '%';
    const el = LL.h('div', {
      class: 'meter' + (thin ? ' thin' : '') + (level ? ' ' + level : ''),
      role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': Math.round(p),
    }, fill);
    return { el, level, pct: p };
  };

  LL.levelNote = (level, pct) => {
    if (level === 'crit') return LL.h('span', { class: 'level-note crit' }, pct >= 100 ? '已用完' : '快用完了');
    if (level === 'warn') return LL.h('span', { class: 'level-note warn' }, '接近上限');
    return null;
  };

  LL.statusBadge = (status) => {
    if (status === 'ok') return LL.h('span', { class: 'badge ok', title: 'ok' }, '✓ 成功');
    if (status === 'cancelled') return LL.h('span', { class: 'badge warn', title: 'cancelled' }, '— 取消');
    return LL.h('span', { class: 'badge err', title: status || 'error' }, '✕ 錯誤');
  };

  const SOURCES = {
    backend: ['後端回報', '後端 (backend) 回報的實際 token 數'],
    estimate: ['估算', '後端沒有回報用量，依字數估算 (estimate)'],
    none: ['無', '沒有計算 token (none)'],
  };
  LL.sourceLabel = (src) => {
    const s = SOURCES[src] || [src || '—', ''];
    return LL.h('span', { class: src === 'estimate' ? 'badge warn' : 'badge', title: s[1] }, s[0]);
  };

  LL.viaLabel = (row) => {
    if (row.via === 'playground') return LL.h('span', { class: 'badge info' }, '試玩 playground');
    return LL.h('span', { class: 'key-display', title: row.key_id || '' }, row.via_label || row.key_id || '—');
  };

  /** Table builder. columns: [{label, cls, cell(row) -> node|string}] */
  LL.table = (columns, rows, emptyText) => {
    const thead = LL.h('thead', null, LL.h('tr', null,
      columns.map((c) => LL.h('th', { class: c.cls || null, scope: 'col' }, c.label))));
    const tbody = LL.h('tbody');
    if (!rows.length) {
      tbody.appendChild(LL.h('tr', null,
        LL.h('td', { class: 'empty', colspan: columns.length }, emptyText || '沒有資料')));
    }
    for (const r of rows) {
      tbody.appendChild(LL.h('tr', null, columns.map((c) => {
        const v = c.cell(r);
        return LL.h('td', { class: c.cls || null }, v === undefined || v === null ? '' : v);
      })));
    }
    return LL.h('table', null, thead, tbody);
  };

  /** Recent-requests table, shared by dashboard and admin. */
  LL.requestsTable = (rows, withUser) => {
    const cols = [{ label: '時間', cls: 'nowrap', cell: (r) => LL.timeEl(r.ts) }];
    if (withUser) cols.push({ label: '帳號', cell: (r) => r.username });
    cols.push(
      { label: '模型 (model)', cell: (r) => LL.h('span', { class: 'mono', title: r.backend ? '後端: ' + r.backend : '' }, r.model) },
      {
        label: 'Tokens', cls: 'num', cell: (r) => LL.h('span', {
          title: 'prompt ' + LL.fmtNum(r.prompt_tokens) + ' + completion ' + LL.fmtNum(r.completion_tokens),
        }, LL.fmtNum(r.total_tokens)),
      },
      { label: '計算方式', cell: (r) => LL.sourceLabel(r.usage_source) },
      {
        label: '狀態', cell: (r) => {
          const b = LL.statusBadge(r.status);
          if (r.error) b.setAttribute('title', r.error);
          return b;
        },
      },
      { label: '耗時', cls: 'num', cell: (r) => LL.fmtDuration(r.duration_ms) + (r.stream ? ' · 串流' : '') },
      { label: '途徑 (via)', cell: (r) => LL.viaLabel(r) },
    );
    return LL.table(cols, rows, '還沒有任何請求 (no requests yet)');
  };

  /** Shows the first `limit` rows with a "show all" toggle below the table. */
  LL.limitedTable = (box, rows, limit, makeTable) => {
    let all = false;
    const render = () => {
      LL.fill(box, makeTable(all ? rows : rows.slice(0, limit)));
      if (rows.length > limit) {
        const btn = LL.h('button', { type: 'button', class: 'btn small ghost' },
          all ? '只顯示最近 ' + limit + ' 筆' : '顯示全部 ' + rows.length + ' 筆');
        btn.addEventListener('click', () => { all = !all; render(); });
        box.appendChild(LL.h('div', { class: 'more-row' }, btn));
      }
    };
    render();
  };

  /** Fills the sticky top bar: brand, nav, user + logout. */
  LL.renderTopbar = (me, active) => {
    const bar = document.getElementById('topbar');
    const title = me.title || 'llmop';
    document.title = (active === 'admin' ? '管理' : '儀表板') + ' · ' + title;
    const nav = LL.h('nav', { class: 'nav', 'aria-label': '主選單' },
      LL.h('a', { href: '/ui/dashboard', class: active === 'dashboard' ? 'active' : null }, '儀表板'),
      me.is_admin ? LL.h('a', { href: '/ui/admin', class: active === 'admin' ? 'active' : null }, '管理') : null);
    const logout = LL.h('button', { type: 'button', class: 'btn small' }, '登出');
    logout.addEventListener('click', LL.logout);
    LL.fill(bar,
      LL.h('a', { class: 'brand', href: '/ui/dashboard' },
        LL.h('img', { src: '/ui/static/favicon.svg', alt: '' }),
        LL.h('span', { class: 'brand-name' }, title)),
      nav,
      LL.h('div', { class: 'user-box' },
        LL.h('div', { class: 'who' },
          LL.h('strong', null, me.display_name),
          me.is_admin ? ' · 管理員' : '',
          LL.h('small', { title: LL.fmtDateTime(me.session_expires_at) },
            '登入有效至 ' + LL.fmtShortDateTime(me.session_expires_at))),
        logout));
  };

  LL.showFatal = (container, err) => {
    LL.fill(container, LL.h('div', { class: 'notice err', role: 'alert' },
      '載入失敗 (failed to load)：' + (err && err.message ? err.message : String(err))));
  };

  /** Run the page's main function once the DOM is ready. */
  LL.boot = (fn) => {
    const run = () => Promise.resolve().then(fn).catch((e) => {
      if (e && e.status === 401) return;
      const main = document.querySelector('main');
      if (main) main.prepend(LL.h('div', { class: 'notice err', role: 'alert' },
        '發生錯誤：' + (e && e.message ? e.message : String(e))));
      console.error(e);
    });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
    else run();
  };
})();
