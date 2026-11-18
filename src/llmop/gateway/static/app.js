// 閘道管理介面：純 JavaScript，呼叫 /admin/api/*。
// 所有來自伺服器的字串（名稱、模型名稱等）一律用 textContent 放進頁面，不用 innerHTML。
"use strict";

const API = "/admin/api";
let csrf = null;
let editingKeyId = null;

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// 共用工具
// ---------------------------------------------------------------------------

async function call(method, path, body) {
  const options = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") {
    options.headers["Content-Type"] = "application/json";
    if (csrf) options.headers["X-LLMOP-CSRF"] = csrf;
    options.body = JSON.stringify(body || {});
  }
  const response = await fetch(API + path, options);
  let data = null;
  try {
    data = await response.json();
  } catch (e) {
    data = null;
  }
  if (response.status === 401 && path !== "/login") {
    showLogin();
  }
  if (!response.ok) {
    const message = data && data.error ? data.error.message : "請求失敗（HTTP " + response.status + "）";
    throw new Error(message);
  }
  return data;
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function row(cells) {
  const tr = document.createElement("tr");
  for (const cell of cells) {
    const td = document.createElement("td");
    if (cell instanceof Node) td.appendChild(cell);
    else td.textContent = cell === null || cell === undefined ? "" : String(cell);
    tr.appendChild(td);
  }
  return tr;
}

function button(label, onClick) {
  const b = el("button", label);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

function formatTime(seconds) {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleString();
}

function limitText(value) {
  return value === null || value === undefined ? "不限" : String(value);
}

function parseLimit(text) {
  const trimmed = text.trim();
  return trimmed === "" ? null : Number.parseInt(trimmed, 10);
}

function notify(text, ok) {
  const box = $("message");
  box.textContent = text;
  box.className = ok ? "message ok" : "message";
  box.hidden = false;
}

function clearNotice() {
  $("message").hidden = true;
}

function showSecret(title, value) {
  $("secret-title").textContent = title;
  $("secret-value").textContent = value;
  $("secret-box").hidden = false;
  $("secret-box").scrollIntoView();
}

async function guarded(action) {
  clearNotice();
  try {
    await action();
  } catch (e) {
    notify(e.message, false);
  }
}

// ---------------------------------------------------------------------------
// 登入
// ---------------------------------------------------------------------------

function showLogin() {
  csrf = null;
  $("main-view").hidden = true;
  $("logout").hidden = true;
  $("login-view").hidden = false;
}

async function showMain() {
  $("login-view").hidden = true;
  $("main-view").hidden = false;
  $("logout").hidden = false;
  await refreshAll();
}

async function refreshAll() {
  await Promise.all([loadKeys(), loadWorkers(), loadUsage()]);
}

// ---------------------------------------------------------------------------
// API 金鑰
// ---------------------------------------------------------------------------

async function loadKeys() {
  const data = await call("GET", "/keys");
  const body = $("keys-body");
  body.replaceChildren();
  for (const key of data.keys) {
    const actions = document.createElement("div");
    if (!key.revoked_at) {
      actions.appendChild(button("編輯", () => startEdit(key)));
      actions.appendChild(button("撤銷", () => guarded(() => revokeKey(key))));
    }
    const today = key.today || { requests: 0, prompt_tokens: 0, completion_tokens: 0 };
    body.appendChild(row([
      el("span", key.id, "mono"),
      key.name,
      key.sig_key_id ? el("span", key.sig_key_id, "mono") : el("span", "未設定", "revoked"),
      limitText(key.daily_request_limit),
      limitText(key.daily_token_limit),
      today.requests,
      today.prompt_tokens + " / " + today.completion_tokens,
      key.revoked_at ? el("span", "已撤銷（" + formatTime(key.revoked_at) + "）", "revoked") : "使用中",
      actions,
    ]));
  }
}

function startEdit(key) {
  editingKeyId = key.id;
  const form = $("key-edit-form");
  $("key-edit-id").textContent = key.id + "（" + key.name + "）";
  form.elements.sig_public_key.value = key.sig_public_key || "";
  form.elements.daily_request_limit.value = key.daily_request_limit === null ? "" : key.daily_request_limit;
  form.elements.daily_token_limit.value = key.daily_token_limit === null ? "" : key.daily_token_limit;
  form.hidden = false;
  form.scrollIntoView();
}

async function revokeKey(key) {
  if (!confirm("確定要撤銷 API 金鑰 " + key.id + "（" + key.name + "）？此動作無法復原。")) return;
  await call("POST", "/keys/" + encodeURIComponent(key.id) + "/revoke");
  notify("已撤銷 " + key.id, true);
  await loadKeys();
}

async function createKey(event) {
  event.preventDefault();
  const form = event.target;
  const sig = form.elements.sig_public_key.value.trim();
  const data = await call("POST", "/keys", {
    name: form.elements.name.value,
    sig_public_key: sig === "" ? null : sig,
    daily_request_limit: parseLimit(form.elements.daily_request_limit.value),
    daily_token_limit: parseLimit(form.elements.daily_token_limit.value),
  });
  form.reset();
  showSecret("新的 API 金鑰：" + data.key.id + "（" + data.key.name + "）", data.api_key);
  await loadKeys();
}

async function saveKey(event) {
  event.preventDefault();
  const form = event.target;
  const sig = form.elements.sig_public_key.value.trim();
  await call("PATCH", "/keys/" + encodeURIComponent(editingKeyId), {
    sig_public_key: sig === "" ? null : sig,
    daily_request_limit: parseLimit(form.elements.daily_request_limit.value),
    daily_token_limit: parseLimit(form.elements.daily_token_limit.value),
  });
  form.hidden = true;
  notify("已更新 " + editingKeyId, true);
  editingKeyId = null;
  await loadKeys();
}

// ---------------------------------------------------------------------------
// worker
// ---------------------------------------------------------------------------

async function loadWorkers() {
  const data = await call("GET", "/workers");
  const list = $("workers-list");
  list.replaceChildren();
  if (data.workers.length === 0) {
    list.appendChild(el("p", "還沒有 worker。", "hint"));
  }
  for (const worker of data.workers) {
    list.appendChild(renderWorker(worker));
  }
}

function renderWorker(worker) {
  const box = el("div", null, "worker");
  const title = el("h3");
  title.appendChild(el("span", worker.name + " "));
  title.appendChild(el("span", worker.id, "mono"));
  title.appendChild(el("span", " "));
  if (worker.revoked_at) title.appendChild(el("span", "已撤銷", "revoked"));
  else if (worker.online) title.appendChild(el("span", "線上", "online"));
  else title.appendChild(el("span", "離線", "offline"));
  box.appendChild(title);

  const facts = [
    "建立：" + formatTime(worker.created_at),
    "最後連線：" + formatTime(worker.last_seen_at),
  ];
  if (worker.online) facts.push("並行上限：" + worker.max_concurrency + "，排隊中：" + worker.pending);
  if (worker.reg_expires_at) facts.push("註冊權杖有效至：" + formatTime(worker.reg_expires_at));
  box.appendChild(el("p", facts.join("　")));

  box.appendChild(el("p", "模型：" + (worker.models.length ? worker.models.join("、") : "（無）")));

  const table = el("table");
  const head = document.createElement("thead");
  head.appendChild(row(["key_id（指紋）", "公鑰（b64u）", "首次出現", "目前金鑰"]));
  table.appendChild(head);
  const tbody = document.createElement("tbody");
  for (const key of worker.keys) {
    tbody.appendChild(row([
      el("span", key.key_id, "mono"),
      el("span", key.public_key, "mono"),
      formatTime(key.first_seen_at),
      key.key_id === worker.current_key_id ? "是" : "",
    ]));
  }
  if (worker.keys.length === 0) tbody.appendChild(row(["（尚未連線過）", "", "", ""]));
  table.appendChild(tbody);
  const scroll = el("div", null, "scroll");
  scroll.appendChild(table);
  box.appendChild(scroll);

  if (!worker.revoked_at) {
    const actions = el("p");
    actions.appendChild(button("重新發出註冊權杖", () => guarded(() => reissue(worker))));
    actions.appendChild(button("撤銷", () => guarded(() => revokeWorker(worker))));
    box.appendChild(actions);
  }
  return box;
}

async function createWorker(event) {
  event.preventDefault();
  const form = event.target;
  const data = await call("POST", "/workers", { name: form.elements.name.value });
  form.reset();
  showSecret("worker " + data.worker.name + "（" + data.worker.id + "）的註冊權杖，一小時內有效、只能用一次",
    data.reg_token);
  await loadWorkers();
}

async function reissue(worker) {
  if (!confirm("為 " + worker.name + " 重新發出註冊權杖？舊的註冊權杖會失效。")) return;
  const data = await call("POST", "/workers/" + encodeURIComponent(worker.id) + "/reg-token");
  showSecret("worker " + worker.name + "（" + worker.id + "）的新註冊權杖，一小時內有效、只能用一次",
    data.reg_token);
  await loadWorkers();
}

async function revokeWorker(worker) {
  if (!confirm("確定要撤銷 worker " + worker.name + "？它會立刻斷線，且無法再連入。")) return;
  await call("POST", "/workers/" + encodeURIComponent(worker.id) + "/revoke");
  notify("已撤銷 " + worker.id, true);
  await loadWorkers();
}

// ---------------------------------------------------------------------------
// 用量
// ---------------------------------------------------------------------------

async function loadUsage() {
  const days = Number.parseInt($("usage-form").elements.days.value, 10) || 7;
  const data = await call("GET", "/usage?days=" + days);
  const body = $("usage-body");
  body.replaceChildren();
  for (const item of data.usage) {
    body.appendChild(row([
      item.day, el("span", item.api_key_id, "mono"), item.requests, item.prompt_tokens, item.completion_tokens,
    ]));
  }
  if (data.usage.length === 0) body.appendChild(row(["（沒有資料）", "", "", "", ""]));
}

// ---------------------------------------------------------------------------
// 啟動
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    guarded(async () => {
      const data = await call("POST", "/login", { password: event.target.elements.password.value });
      event.target.reset();
      csrf = data.csrf;
      await showMain();
    });
  });
  $("logout").addEventListener("click", () => guarded(async () => {
    await call("POST", "/logout");
    showLogin();
  }));
  $("key-create-form").addEventListener("submit", (event) => guarded(() => createKey(event)));
  $("key-edit-form").addEventListener("submit", (event) => guarded(() => saveKey(event)));
  $("key-edit-cancel").addEventListener("click", () => { $("key-edit-form").hidden = true; });
  $("worker-create-form").addEventListener("submit", (event) => guarded(() => createWorker(event)));
  $("usage-form").addEventListener("submit", (event) => {
    event.preventDefault();
    guarded(loadUsage);
  });
  $("secret-close").addEventListener("click", () => {
    $("secret-value").textContent = "";
    $("secret-box").hidden = true;
  });

  guarded(async () => {
    const data = await call("GET", "/session");
    if (data.authenticated) {
      csrf = data.csrf;
      await showMain();
    } else {
      showLogin();
    }
  });
});
