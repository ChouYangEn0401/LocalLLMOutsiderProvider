/* Portal: tier + status, the two usage tables (1.5-hour bucket, weekly), the current
   API link, the user's own API keys (create / revoke / encryption / signing public
   key / per-key usage), the queue, how-to and recent requests.
   Every server string goes through textContent (LL.h children), never innerHTML. */
(function () {
  'use strict';

  const { h, $, api, fill } = LL;
  let me = null;
  let portal = null;

  const TIER_NAMES = {
    vip3: 'VIP III', vip2: 'VIP II', vip1: 'VIP I', normal: 'NORMAL', guest: 'GUEST',
  };

  LL.boot(async () => {
    me = await LL.loadMe();
    LL.renderTopbar(me, 'dashboard');
    bindKeyForm();
    $('#refresh').addEventListener('click', () => refreshAll(true));
    await refreshAll(false);
    setInterval(() => {
      if (document.visibilityState === 'visible') loadPortal().catch(() => {});
    }, 30000);
  });

  async function refreshAll(withToast) {
    const results = await Promise.allSettled([loadPortal(), loadRecent()]);
    const failed = results.find((r) => r.status === 'rejected');
    if (failed && failed.reason && failed.reason.status !== 401) {
      LL.toast('部分資料載入失敗：' + failed.reason.message, true);
    } else if (withToast) {
      LL.toast('已更新');
    }
  }

  async function loadPortal() {
    try {
      portal = await api('GET', '/ui/api/portal');
    } catch (e) {
      LL.showFatal($('#status-cards'), e);
      throw e;
    }
    renderStatus(portal);
    renderApiLink(portal.api_link);
    renderKeys(portal);
    renderQueue(portal.queue);
    renderHowTo();
  }

  async function loadRecent() {
    const u = await api('GET', '/ui/api/usage?days=7');
    LL.limitedTable($('#recent'), u.recent || [], 15, (rows) => LL.requestsTable(rows, false));
  }

  // ================================================================ status + tables
  function minutes(sec) {
    sec = Number(sec) || 0;
    if (sec < 60) return '不到 1 分鐘';
    if (sec < 3600) return '約 ' + Math.ceil(sec / 60) + ' 分鐘';
    return '約 ' + (sec / 3600).toFixed(1) + ' 小時';
  }

  function card(label, en, valueNode, ...rest) {
    return h('div', { class: 'card stat' },
      h('div', { class: 'stat-label' }, label, ' ', h('small', null, en)),
      h('div', { class: 'stat-value' }, valueNode), rest);
  }

  function renderStatus(p) {
    const st = p.status || {};
    const tier = TIER_NAMES[p.tier] || String(p.tier || '');
    const low = st.state === 'LOW';
    const stateNode = h('span', { class: 'badge ' + (low ? 'warn' : 'ok') }, low ? 'LOW' : 'NORMAL');
    let stateFoot = low ? '桶回到 25% 後恢復 NORMAL' : '正常排隊';
    if (low && st.bucket && st.bucket.seconds_to_recover) {
      stateFoot = minutes(st.bucket.seconds_to_recover) + '後恢復 NORMAL';
    }
    if (st.allowed === false && st.retry_after) {
      stateFoot = '暫時不能送出，' + minutes(st.retry_after) + '後再試';
    }

    const cards = [
      card('等級', '(tier)', tier, h('div', { class: 'stat-foot' }, p.model ? '模型：' + p.model : '')),
      card('狀態', '(status)', stateNode, h('div', { class: 'stat-foot' }, stateFoot)),
    ];

    const b = st.bucket;
    if (b) {
      const used = Math.max(0, b.capacity - Math.max(0, b.level));
      const m = LL.meter(used, b.capacity);
      cards.push(card('1.5 小時用量', '(1.5-hour bucket)',
        h('span', null, LL.fmtPct(b.percent), h('span', { class: 'of' }, ' 剩餘')),
        m.el,
        h('div', { class: 'stat-foot' },
          LL.fmtNum(Math.max(0, b.level)) + ' / ' + LL.fmtNum(b.capacity) + ' tokens · ' +
          (b.seconds_to_full ? minutes(b.seconds_to_full) + '後全滿' : '已滿'))));
    } else {
      cards.push(card('1.5 小時用量', '(1.5-hour bucket)', h('span', { class: 'pill' }, '不限 (unlimited)')));
    }

    const w = st.weekly;
    if (w && w.limit) {
      const m = LL.meter(w.used, w.limit);
      cards.push(card('每週用量', '(weekly)',
        h('span', null, LL.fmtNum(w.used), h('span', { class: 'of' }, ' / ' + LL.fmtNum(w.limit))),
        m.el,
        h('div', { class: 'stat-foot' }, LL.levelNote(m.level, m.pct),
          w.resets_at ? ' ' + LL.fmtShortDateTime(w.resets_at) + ' 重置' : '')));
    } else {
      cards.push(card('每週用量', '(weekly)',
        h('span', null, LL.fmtNum(w ? w.used : 0), ' ', h('span', { class: 'pill' }, '不限')),
        h('div', { class: 'stat-foot' }, w && w.resets_at ? LL.fmtShortDateTime(w.resets_at) + ' 重置' : '')));
    }
    if (!p.status) {
      cards.push(h('div', { class: 'card stat muted' }, '用量資料暫時無法取得 (usage unavailable)'));
    }
    fill($('#status-cards'), cards);
  }

  // ================================================================ API link
  function copyLine(id, label, value) {
    const input = h('input', { type: 'text', readonly: true, id, spellcheck: 'false' });
    input.value = value;
    input.addEventListener('focus', () => input.select());
    return [h('label', { for: id }, label), h('div', { class: 'copy-line' }, input, LL.copyButton(() => value))];
  }

  function renderApiLink(link) {
    const box = $('#api-link');
    if (!link || !link.url) {
      fill(box, h('p', { class: 'muted' },
        '目前沒有 API 連結 (no API link yet)。請稍後再看，或聯絡管理員。'));
      return;
    }
    fill(box,
      copyLine('link-v1', '明文 OpenAI 相容 base_url（/v1）', link.v1_base_url),
      copyLine('link-e2e', '加密用 gateway_url（給 llmop client，/e2e/v1 由外掛自動加）', link.url),
      link.updated_at ? h('p', { class: 'small muted' }, '連結更新於 ', LL.timeEl(link.updated_at)) : null);
  }

  // ================================================================ keys
  function encBadge(k) {
    return k.encryption === 'required'
      ? h('span', { class: 'badge info', title: '只能經加密路徑 /e2e/v1 使用' }, '一定加密')
      : h('span', { class: 'badge', title: '明文 /v1 與加密 /e2e/v1 都可用' }, '可不加密');
  }

  function keyUsage(k) {
    const u = k.usage || {};
    const week = u.week || { total: 0, requests: 0 };
    const all = u.all || { total: 0, requests: 0 };
    return h('span', { class: 'num', title: '全部 ' + LL.fmtNum(all.total) + ' tokens / ' + LL.fmtNum(all.requests) + ' 次' },
      LL.fmtNum(week.total), h('span', { class: 'muted' }, ' 本週'));
  }

  function renderKeys(p) {
    const keys = p.keys || [];
    $('#keys-count').textContent = '使用中 ' + p.active_count + ' / 上限 ' + p.max_api_keys + ' 把';
    $('#key-create').disabled = p.active_count >= p.max_api_keys;
    fill($('#keys-table'), LL.table([
      { label: '名稱', cell: (k) => k.name },
      { label: '金鑰', cell: (k) => h('span', { class: 'key-display mono' }, k.display) },
      { label: '加密', cell: (k) => encBadge(k) },
      { label: '簽章公鑰', cell: (k) => k.sig_key_id ? h('span', { class: 'mono small', title: k.sig_public_key || '' }, k.sig_key_id) : h('span', { class: 'muted' }, '—') },
      { label: '用量', cls: 'num', cell: (k) => keyUsage(k) },
      { label: '最後使用', cell: (k) => LL.timeEl(k.last_used_at, '尚未使用') },
      { label: '到期', cell: (k) => k.expires_at ? LL.timeEl(k.expires_at) : h('span', { class: 'muted' }, '永不') },
      { label: '', cls: 'right nowrap', cell: (k) => keyActions(k) },
    ], keys, '還沒有金鑰 (no keys yet)'));
  }

  function keyActions(k) {
    const toggle = h('button', { type: 'button', class: 'btn small' },
      k.encryption === 'required' ? '改為可不加密' : '改為一定加密');
    toggle.addEventListener('click', () => updateKey(k, {
      encryption: k.encryption === 'required' ? 'optional' : 'required',
    }, toggle));
    const sig = h('button', { type: 'button', class: 'btn small' }, '設定簽章公鑰');
    sig.addEventListener('click', () => {
      const value = window.prompt('貼上 llmop client keygen 產生的簽章公鑰（留空 = 移除）', k.sig_public_key || '');
      if (value === null) return;
      updateKey(k, { sig_public_key: value.trim() || null }, sig);
    });
    const revoke = h('button', { type: 'button', class: 'btn small danger' }, '撤銷');
    revoke.addEventListener('click', () => revokeKey(k, revoke));
    return [toggle, ' ', sig, ' ', revoke];
  }

  async function updateKey(k, body, btn) {
    btn.disabled = true;
    try {
      await api('PATCH', '/ui/api/keys/' + encodeURIComponent(k.id), body);
      LL.toast('已更新金鑰');
    } catch (e) {
      LL.toast(e.message, true);
    }
    btn.disabled = false;
    await loadPortal().catch(() => {});
  }

  async function revokeKey(k, btn) {
    if (!window.confirm('確定撤銷「' + (k.name || k.display) + '」？使用這把金鑰的程式會立刻失效。')) return;
    btn.disabled = true;
    try {
      await api('DELETE', '/ui/api/keys/' + encodeURIComponent(k.id));
      LL.toast('已撤銷');
    } catch (e) {
      LL.toast(e.message, true);
      btn.disabled = false;
    }
    await loadPortal().catch(() => {});
  }

  function bindKeyForm() {
    const form = $('#key-form');
    const err = $('#key-error');
    const btn = $('#key-create');
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      err.textContent = '';
      const body = { name: $('#key-name').value.trim(), encryption: $('#key-encryption').value };
      const exp = $('#key-expires').value;
      if (exp) body.expires_days = Number(exp);
      const sig = $('#key-sig').value.trim();
      if (sig) body.sig_public_key = sig;
      btn.disabled = true;
      try {
        const r = await api('POST', '/ui/api/keys', body);
        form.reset();
        $('#key-sig').value = '';
        showNewKey(r.key, r.record);
      } catch (e) {
        err.textContent = e.message;
      }
      btn.disabled = false;
      await loadPortal().catch(() => {});
    });
  }

  function showNewKey(key, rec) {
    const box = $('#new-key');
    const input = h('input', { type: 'text', readonly: true, 'aria-label': '新的 API 金鑰', spellcheck: 'false' });
    input.value = key;
    input.addEventListener('focus', () => input.select());
    const done = h('button', { type: 'button', class: 'btn' }, '我已複製，關閉');
    done.addEventListener('click', () => {
      input.value = '';
      fill(box);
    });
    fill(box, h('div', { class: 'secret-box', role: 'alert' },
      h('h3', null, '新的 API 金鑰「' + (rec.name || rec.display) + '」已建立'),
      h('p', { class: 'small' },
        h('strong', null, '這是唯一一次顯示完整金鑰。'),
        '請立即複製並妥善保存（像密碼一樣）。關閉後就無法再看到，只能撤銷後重新申請。'),
      h('div', { class: 'copy-line' }, input, LL.copyButton(() => key)),
      done));
    input.focus();
  }

  // ================================================================ queue
  function renderQueue(q) {
    const box = $('#queue');
    if (!q || !q.available) {
      fill(box, h('p', { class: 'muted' }, '排隊資訊暫時無法取得 (queue status unavailable)。'));
      return;
    }
    const mine = q.mine || {};
    fill(box, h('dl', { class: 'kv' },
      h('dt', null, '執行中 / 名額'), h('dd', null, LL.fmtNum(q.running) + ' / ' + LL.fmtNum(q.slots)),
      h('dt', null, '排隊中'), h('dd', null, LL.fmtNum(q.waiting) + ' 筆'),
      h('dt', null, '預估等待'), h('dd', null, q.waiting ? minutes(q.eta_seconds) : '不用等'),
      h('dt', null, '我的請求'), h('dd', null,
        (mine.running || 0) + ' 筆執行中、' + (mine.waiting || 0) + ' 筆排隊' +
        (mine.position ? '（第 ' + mine.position + ' 位）' : ''))));
  }

  // ================================================================ how-to
  function renderHowTo() {
    const link = portal && portal.api_link;
    const apiBase = link && link.v1_base_url ? link.v1_base_url : '（目前沒有 API 連結）';
    const keyHint = 'sk-llop-你的金鑰';
    const model = (portal && portal.model) || 'any';
    const curl =
      'curl ' + apiBase + '/chat/completions \\\n' +
      '  -H "Authorization: Bearer ' + keyHint + '" \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      "  -d '{\"model\": " + JSON.stringify(model) + ', "messages": [{"role": "user", "content": "你好"}]}\'\n\n' +
      '# 查詢自己的等級與用量\n' +
      'curl ' + apiBase + '/me -H "Authorization: Bearer ' + keyHint + '"';
    const python =
      '# pip install openai\n' +
      'from openai import OpenAI\n\n' +
      'client = OpenAI(base_url=' + JSON.stringify(apiBase) + ', api_key="' + keyHint + '")\n' +
      'resp = client.chat.completions.create(\n' +
      '    model=' + JSON.stringify(model) + ',  # 伺服器只有一個模型，這個欄位會被忽略\n' +
      '    messages=[{"role": "user", "content": "你好"}],\n' +
      ')\n' +
      'print(resp.choices[0].message.content)';
    const encrypted =
      '# 加密模式：需要 llmop 用戶端外掛\n' +
      'llmop client keygen            # 產生簽章金鑰，把公鑰貼到上面的金鑰設定\n' +
      '# 用戶端設定檔的 gateway_url 設成：' + (link && link.url ? link.url : '<API 連結>') + '\n' +
      'llmop client proxy             # 再讓程式連到外掛印出的本機位址';
    fill($('#howto'),
      h('p', null, '1. 申請一把金鑰。2. 把 API 連結的 base_url 和金鑰填進程式。伺服器只有一個模型，不必選。'),
      h('p', { class: 'small muted' }, '「一定加密」的金鑰只能經 llmop 用戶端外掛使用；明文 /v1 會被拒絕。'),
      LL.codeBlock(curl), LL.codeBlock(python), LL.codeBlock(encrypted));
  }
})();
