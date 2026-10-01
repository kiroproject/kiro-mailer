// KIRO mailer: admin application (frontend host API v1).
// Mailings list, composer (recipients, message, media, buttons, poll), scheduling, delivery statistics.
const API = "/api/admin/kiro-mailer";

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const ERRORS = {
  forbidden: "Нет прав администратора",
  csrf_failed: "Сессия устарела, обновите страницу",
  not_found: "Не найдено",
  bad_state: "Действие недоступно в текущем состоянии рассылки",
  content_invalid: "В рассылке есть ошибки",
  schedule_in_past: "Время должно быть в будущем",
  invalid_time: "Укажите дату и время",
  media_too_large: "Файл слишком большой",
  unsupported_file: "Этот файл не подходит по типу",
  name_required: "Укажите название",
  test_failed: "Не удалось отправить тест",
  admin_telegram_unavailable: "Не найден ваш Telegram-аккаунт",
  not_ready: "Плагин ещё запускается, повторите через минуту",
  chunk_out_of_order: "Сбой загрузки файла, попробуйте ещё раз",
};

const STATUS = {
  draft: ["Черновик", "#8b93a3"],
  scheduled: ["Запланирована", "#3b82f6"],
  sending: ["Отправляется", "#22c55e"],
  paused: ["Пауза", "#f59e0b"],
  done: ["Завершена", "#6366f1"],
  cancelled: ["Отменена", "#6b7280"],
};
const RSTATUS = { pending: "Ожидает", sent: "Доставлено", failed: "Ошибка", blocked: "Заблокировал бота", skipped: "Пропущено" };
const MEDIA_RU = { photo: "Фото", video: "Видео", animation: "GIF", document: "Файл" };
const SECTIONS_RU = { home: "Главная", plans: "Тарифы", install: "Подключение", trial: "Пробный период", invite: "Бонусы", devices: "Устройства", support: "Поддержка", settings: "Настройки" };

function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)rw_webapp_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function api(path, method = "GET", body, extraHeaders) {
  const headers = { Accept: "application/json", ...(extraHeaders || {}) };
  const raw = body instanceof Uint8Array || body instanceof Blob;
  if (body !== undefined && !raw) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrf();
  const res = await fetch(API + path, {
    method,
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    const err = new Error(ERRORS[data.error] || data.detail || data.error || `HTTP ${res.status}`);
    err.code = data.error;
    err.data = data;
    throw err;
  }
  return data;
}

const num = (v) => Number(v || 0).toLocaleString("ru-RU");
const pct = (v) => `${Number(v || 0).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
const fmtDate = (s) => {
  if (!s) return "—";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" });
};
const fmtDur = (sec) => {
  if (sec === null || sec === undefined) return "—";
  if (sec < 60) return `${sec} с`;
  if (sec < 3600) return `${Math.round(sec / 60)} мин`;
  return `${(sec / 3600).toFixed(1)} ч`;
};
const fmtSize = (b) => (b > 1048576 ? `${(b / 1048576).toFixed(1)} МБ` : `${Math.max(1, Math.round(b / 1024))} КБ`);
const clone = (v) => JSON.parse(JSON.stringify(v));
const h = (html) => {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
};
const debounce = (fn, ms) => {
  let t = 0;
  const wrapped = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
};

const STYLE = `
.km{display:flex;flex-direction:column;gap:14px;color:var(--text);font-size:14px;min-width:0}
.km *{box-sizing:border-box}
.km [data-body],.km [data-main],.km [data-page],.km [data-stats]{display:flex;flex-direction:column;gap:14px;min-width:0}
.km h2,.km h3,.km h4{margin:0}
.km-card{border:1px solid var(--border);border-radius:var(--radius-card,var(--radius,12px));padding:14px;background:var(--panel,var(--bg,transparent));min-width:0}
.km-card>h3{font-size:15px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.km-muted{color:var(--muted);font-size:12.5px}
.km-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.km-grow{flex:1}
.km-btn{border:1px solid var(--border);background:transparent;color:var(--text);border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit;white-space:nowrap}
.km-btn:hover{border-color:var(--accent)}
.km-btn[disabled]{opacity:.5;cursor:default}
.km-primary{background:var(--accent);border-color:var(--accent);color:var(--accent-contrast,#fff)}
.km-danger{color:#e5484d}
.km-tabs{display:flex;gap:6px;flex-wrap:wrap}
.km-tab{border:1px solid var(--border);background:transparent;color:var(--muted);border-radius:999px;padding:6px 14px;cursor:pointer;font:inherit}
.km-tab.on{color:var(--text);border-color:var(--accent);background:color-mix(in srgb,var(--accent) 14%,transparent)}
.km input,.km select,.km textarea{font:inherit;color:var(--text);background-color:var(--panel-2,var(--panel,#1f2430));border:1px solid var(--border);border-radius:var(--radius-control,8px);padding:7px 9px;color-scheme:inherit;min-width:0;max-width:100%}
.km select option{background-color:var(--panel-2,var(--panel,#1f2430));color:var(--text)}
.km input[type=checkbox],.km input[type=radio]{accent-color:var(--accent);width:auto}
.km textarea{resize:vertical;width:100%;min-height:96px}
.km label.f{display:flex;flex-direction:column;gap:4px;color:var(--muted);font-size:12px}
.km label.chk{display:flex;align-items:center;gap:8px;color:var(--text);font-size:13.5px;cursor:pointer}
.km-table{width:100%;border-collapse:collapse}
.km-table th,.km-table td{padding:8px 6px;border-bottom:1px solid var(--border);text-align:left;vertical-align:middle}
.km-table th{color:var(--muted);font-weight:500;font-size:12px}
.km-scroll{overflow-x:auto}
.km-badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;border:1px solid currentColor;white-space:nowrap}
.km-banner{padding:10px 12px;border-radius:10px;border:1px solid;font-size:13px;line-height:1.45}
.km-banner.warn{border-color:#f59e0b;background:rgba(245,158,11,.1)}
.km-banner.ok{border-color:#22c55e;background:rgba(34,197,94,.08)}
.km-banner.bad{border-color:#e5484d;background:rgba(229,72,77,.08)}
.km-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.km-stat{border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.km-stat b{display:block;font-size:20px;margin-top:2px}
.km-bar{height:10px;border-radius:999px;background:rgba(127,127,127,.2);overflow:hidden;display:flex}
.km-bar i{display:block;height:100%}
.km-chips{display:flex;gap:6px;flex-wrap:wrap}
.km-chip{border:1px solid var(--border);background:transparent;color:var(--text);border-radius:999px;padding:5px 12px;cursor:pointer;font:inherit;font-size:13px}
.km-chip.on{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 16%,transparent)}
.km-sub{border:1px dashed var(--border);border-radius:10px;padding:10px;margin-top:10px}
.km-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px 14px}
.km-compose{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:14px;align-items:start}
.km-side{position:sticky;top:8px;display:flex;flex-direction:column;gap:12px}
.km-count{font-size:30px;font-weight:700;line-height:1.1}
.km-msg{padding:8px 12px;border-radius:8px;background:rgba(229,72,77,.12);color:#e5484d}
.km-modal{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:2147483000;display:flex;align-items:center;justify-content:center;padding:16px}
.km-modal>div{background:var(--panel,#1b1f2a);color:var(--text);border:1px solid var(--border);border-radius:14px;padding:18px;max-width:520px;width:100%;max-height:90vh;overflow:auto}
.km-media{display:flex;gap:10px;align-items:center;border:1px solid var(--border);border-radius:10px;padding:8px;margin-top:8px}
.km-media img,.km-media .ph{width:54px;height:54px;border-radius:8px;object-fit:cover;background:rgba(127,127,127,.2);display:flex;align-items:center;justify-content:center;flex:none}
.km-prog{height:6px;border-radius:99px;background:rgba(127,127,127,.2);overflow:hidden;margin-top:4px}
.km-prog i{display:block;height:100%;background:var(--accent)}
/* Telegram-like preview */
.tg{background:#0e1621;border-radius:14px;padding:12px;color:#fff;font-size:14px}
.tg-bubble{background:#182533;border-radius:12px 12px 12px 4px;padding:8px 10px;max-width:100%;overflow-wrap:anywhere;line-height:1.4}
.tg-bubble+.tg-bubble{margin-top:6px}
.tg-bubble a{color:#6ab3f3}
.tg-bubble code,.tg-bubble pre{background:rgba(255,255,255,.08);border-radius:4px;padding:0 4px}
.tg-media{background:#243447;border-radius:8px;margin-bottom:6px;overflow:hidden;font-size:12px;color:#9fb3c8}
.tg-media img{width:100%;display:block;max-height:220px;object-fit:cover}
.tg-media .ph{padding:22px;text-align:center}
.tg-kb{display:flex;flex-direction:column;gap:4px;margin-top:6px}
.tg-kb div{background:#2b5278;border-radius:8px;text-align:center;padding:7px;font-size:13px}
.tg-poll{font-size:13.5px}
.tg-poll b{display:block;margin-bottom:6px}
.tg-poll div.o{display:flex;gap:8px;align-items:center;padding:3px 0}
.tg-poll div.o i{width:14px;height:14px;border:2px solid #6ab3f3;border-radius:50%;flex:none}
.tg-poll div.o i.sq{border-radius:3px}
@media (max-width:1100px){.km-compose{grid-template-columns:1fr}.km-side{position:static}}
`;

function modal(html, onMount) {
  const m = h(`<div class="km-modal"><div>${html}</div></div>`);
  document.body.appendChild(m);
  const close = () => m.remove();
  m.addEventListener("pointerdown", (e) => { if (e.target === m) close(); });
  onMount?.(m, close);
  return { el: m, close };
}

function confirmBox(htmlText, okLabel = "Продолжить") {
  return new Promise((resolve) => {
    modal(`<p style="margin:0 0 14px;line-height:1.5">${htmlText}</p><div class="km-row" style="justify-content:flex-end"><button class="km-btn" data-no>Отмена</button><button class="km-btn km-primary" data-ok>${esc(okLabel)}</button></div>`, (m, close) => {
      m.querySelector("[data-no]").onclick = () => { close(); resolve(false); };
      m.querySelector("[data-ok]").onclick = () => { close(); resolve(true); };
    });
  });
}

function toast(text, kind = "ok") {
  const t = h(`<div class="km-banner ${kind}" style="position:fixed;right:16px;bottom:16px;z-index:2147483001;max-width:380px">${esc(text)}</div>`);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4500);
}

function downloadCsv(name, rows) {
  const csv = rows.map((r) => r.map((c) => `"${String(c ?? "").replace(/"/g, '""')}"`).join(";")).join("\n");
  const url = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

const badge = (status) => {
  const [label, color] = STATUS[status] || [status, "#888"];
  return `<span class="km-badge" style="color:${color}">${esc(label)}</span>`;
};

// Telegram-HTML preview: only the tags Telegram supports survive.
function safeHtml(src) {
  const doc = new DOMParser().parseFromString(`<div>${src}</div>`, "text/html");
  const allowed = new Set(["B", "STRONG", "I", "EM", "U", "INS", "S", "STRIKE", "DEL", "CODE", "PRE", "A", "BR", "TG-SPOILER"]);
  const walk = (node) => {
    [...node.childNodes].forEach((c) => {
      if (c.nodeType === 1) {
        if (!allowed.has(c.tagName)) {
          c.replaceWith(document.createTextNode(c.textContent));
          return;
        }
        [...c.attributes].forEach((a) => { if (!(c.tagName === "A" && a.name === "href")) c.removeAttribute(a.name); });
        if (c.tagName === "A") { c.setAttribute("target", "_blank"); c.setAttribute("rel", "noopener noreferrer"); }
        walk(c);
      }
    });
  };
  walk(doc.body.firstChild);
  return doc.body.firstChild.innerHTML.replace(/\n/g, "<br>");
}

// ---------------------------------------------------------------------------------------------
// Application
// ---------------------------------------------------------------------------------------------
const cache = { tab: "mailings", mid: null };
const EDITABLE = ["draft", "scheduled"];

function localInputValue(offsetHours, plusMinutes = 60) {
  const d = new Date(Date.now() + offsetHours * 3600e3 + plusMinutes * 60e3);
  return d.toISOString().slice(0, 16);
}

async function prepareImage(file) {
  if (/gif$/i.test(file.type)) return { blob: file, kind: "animation" };
  if (file.size <= 9 * 1048576 && /^image\/(jpeg|png|webp)$/.test(file.type)) return { blob: file, kind: "photo" };
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 2560 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.9));
  return { blob, kind: "photo" };
}

async function uploadFile(file, meta, onProgress) {
  let blob = file;
  let kind = "document";
  if (file.type.startsWith("image/")) ({ blob, kind } = await prepareImage(file));
  else if (file.type.startsWith("video/")) kind = "video";
  if (blob.size > meta.limits.media_bytes[kind]) throw new Error(`Файл «${file.name}» больше ${fmtSize(meta.limits.media_bytes[kind])}`);
  const init = await api("/media", "POST", { kind, filename: file.name, content_type: blob.type || file.type, size: blob.size });
  const chunk = Math.min(init.chunk, 3 * 1048576);
  for (let offset = 0; offset < blob.size; offset += chunk) {
    const part = new Uint8Array(await blob.slice(offset, offset + chunk).arrayBuffer());
    await api(`/media/${init.id}/chunk`, "POST", part, { "X-Offset": String(offset), "Content-Type": "application/octet-stream" });
    onProgress?.(Math.min(1, (offset + chunk) / blob.size));
  }
  await api(`/media/${init.id}/finish`, "POST", {});
  return { id: init.id, kind, name: file.name, size: blob.size };
}

function mountApp(target) {
  const root = document.createElement("div");
  root.className = "km";
  root.innerHTML = `<style>${STYLE}</style><div data-body class="km-muted">Загрузка…</div>`;
  target.replaceChildren(root);
  const st = { root, disposed: false, meta: null, m: null, saveTimer: 0, pollTimer: 0, previewSeq: 0, tab: cache.tab, files: {} };
  const body = () => root.querySelector("[data-body]");
  const fail = (err) => toast(err.message || String(err), "bad");

  async function loadMeta(force = false) {
    if (!st.meta || force) st.meta = await api("/meta");
    return st.meta;
  }

  function stop() {
    clearInterval(st.pollTimer);
    clearTimeout(st.saveTimer);
    st.pollTimer = 0;
  }

  function shell(inner) {
    const tabs = [["mailings", "Рассылки"], ["lists", "Списки получателей"], ["settings", "Настройки"]];
    body().innerHTML = `<div class="km-row"><h2 class="km-grow">Рассылки</h2><div class="km-tabs">${tabs.map(([id, t]) => `<button class="km-tab ${st.tab === id ? "on" : ""}" data-tab="${id}">${t}</button>`).join("")}</div></div><div data-main>${inner}</div>`;
    body().querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", async () => {
      await flush();
      st.tab = b.dataset.tab;
      cache.tab = st.tab;
      cache.mid = null;
      route();
    }));
  }

  async function flush() {
    if (st.m && st.saveTimer) { clearTimeout(st.saveTimer); st.saveTimer = 0; await save().catch(() => {}); }
    stop();
  }

  async function route() {
    if (st.disposed) return;
    stop();
    try {
      await loadMeta(true);
      if (st.tab === "mailings" && cache.mid) return await showMailing(cache.mid);
      if (st.tab === "mailings") return await showList();
      if (st.tab === "lists") return await showLists();
      return await showSettings();
    } catch (err) {
      body().innerHTML = `<div class="km-msg">Ошибка: ${esc(err.message)}</div>`;
    }
  }

  // ------------------------------------------------------------------ list
  async function showList() {
    const { mailings } = await api("/mailings");
    st.m = null;
    shell(`<div class="km-row"><button class="km-btn km-primary" data-new>+ Новая рассылка</button><span class="km-muted">Рассылки уходят только в Telegram. Получатели фиксируются в момент отправки.</span></div>
      <div class="km-card km-scroll">${mailings.length ? `<table class="km-table"><tr><th>Рассылка</th><th>Статус</th><th>Когда</th><th>Получателей</th><th>Доставка</th><th></th></tr>
        ${mailings.map((x) => {
          const s = x.stats || {};
          const total = s.total || 0;
          const done = total ? Math.round(((s.sent || 0) / total) * 100) : 0;
          const when = x.status === "scheduled" ? x.scheduled_local?.replace("T", " ") : fmtDate(x.started_at || x.created_at);
          const kinds = [x.media ? `📎 ${x.media}` : "", x.has_poll ? "📊 опрос" : ""].filter(Boolean).join(" · ");
          return `<tr><td><a href="#" data-open="${x.id}"><b>${esc(x.name)}</b></a><div class="km-muted">${esc((x.preview || "").replace(/<[^>]+>/g, "").slice(0, 70))}${kinds ? ` ${kinds}` : ""}</div></td>
            <td>${badge(x.status)}</td><td>${esc(when || "—")}</td><td>${total ? num(total) : "—"}</td>
            <td style="min-width:150px">${total ? `${num(s.sent)} из ${num(total)}${s.failed ? ` · <span style="color:#e5484d">ошибок ${num(s.failed)}</span>` : ""}${s.blocked ? ` · блок ${num(s.blocked)}` : ""}${s.voted ? ` · 📊 ${num(s.voted)}` : ""}<div class="km-prog"><i style="width:${done}%"></i></div>` : "—"}</td>
            <td><button class="km-btn" data-open="${x.id}">Открыть</button></td></tr>`;
        }).join("")}</table>` : `<p class="km-muted" style="margin:0">Рассылок пока нет. Создайте первую: выберите получателей, напишите сообщение, при желании добавьте медиа, кнопки или опрос.</p>`}</div>`);
    const main = body();
    main.querySelector("[data-new]").onclick = async () => { try { const r = await api("/mailings", "POST", {}); cache.mid = r.id; route(); } catch (e) { fail(e); } };
    main.querySelectorAll("[data-open]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); cache.mid = Number(a.dataset.open); route(); }));
  }

  // ------------------------------------------------------------------ mailing page
  async function showMailing(id) {
    const { mailing } = await api(`/mailings/${id}`);
    st.m = mailing;
    st.files = Object.fromEntries((mailing.content.media || []).map((x) => [x.id, { name: "", size: 0 }]));
    const editable = EDITABLE.includes(mailing.status);
    shell(`<div class="km-card"><div class="km-row"><button class="km-btn" data-back>← К списку</button>
        <input type="text" data-name maxlength="120" value="${esc(mailing.name)}" style="flex:1;min-width:200px;font-weight:600" ${editable ? "" : "disabled"}>
        ${badge(mailing.status)}${mailing.status === "scheduled" ? `<span class="km-muted">на ${esc(mailing.scheduled_local.replace("T", " "))} (UTC${mailing.tz_offset_hours >= 0 ? "+" : ""}${mailing.tz_offset_hours})</span>` : ""}
        <span class="km-muted" data-save-status></span><span class="km-grow"></span><span data-actions class="km-row"></span></div></div>
      <div data-page></div>`);
    const main = body();
    main.querySelector("[data-back]").onclick = async () => { await flush(); cache.mid = null; route(); };
    main.querySelector("[data-name]").addEventListener("input", () => { st.m.name = main.querySelector("[data-name]").value; changed(false); });
    renderActions();
    if (editable) renderComposer(main.querySelector("[data-page]"));
    else await renderStats(main.querySelector("[data-page]"));
  }

  function setStatus(text) {
    const el = body().querySelector("[data-save-status]");
    if (el) el.textContent = text;
  }

  async function save() {
    if (!st.m || !EDITABLE.includes(st.m.status)) return;
    const res = await api(`/mailings/${st.m.id}`, "PUT", { name: st.m.name, content: st.m.content, audience: st.m.audience });
    st.m.issues = res.issues;
    setStatus("Сохранено ✓");
    paintIssues();
  }

  function changed(refreshAudience = true) {
    setStatus("Изменено…");
    clearTimeout(st.saveTimer);
    st.saveTimer = setTimeout(() => { st.saveTimer = 0; save().catch(fail); }, 1500);
    paintPreview();
    if (refreshAudience) refreshCount();
  }

  function renderActions() {
    const box = body().querySelector("[data-actions]");
    const s = st.m.status;
    const btn = (id, label, cls = "") => `<button class="km-btn ${cls}" data-act="${id}">${label}</button>`;
    const sets = {
      draft: btn("test", "Тест мне в Telegram") + btn("schedule", "Запланировать") + btn("send", "Отправить сейчас", "km-primary"),
      scheduled: btn("test", "Тест мне в Telegram") + btn("schedule", "Изменить время") + btn("unschedule", "Снять с планирования") + btn("send", "Отправить сейчас", "km-primary"),
      sending: btn("pause", "Пауза") + btn("cancel", "Отменить", "km-danger"),
      paused: btn("resume", "Продолжить", "km-primary") + btn("cancel", "Отменить", "km-danger"),
      done: btn("retry", "Повторить для неудачных") + btn("copy", "Копия") + btn("copy_new", "Копия для не получивших") + btn("delete", "Удалить", "km-danger"),
      cancelled: btn("copy", "Копия") + btn("delete", "Удалить", "km-danger"),
    };
    box.innerHTML = sets[s] || "";
    box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => act(b.dataset.act, b)));
  }

  async function act(action, btn) {
    const id = st.m.id;
    try {
      if (action === "test") {
        await flushSave();
        btn.disabled = true;
        await api(`/mailings/${id}/test`, "POST", {});
        toast("Тестовая рассылка отправлена вам в Telegram");
        btn.disabled = false;
      } else if (action === "send") {
        await flushSave();
        if (st.m.issues?.length) return toast(st.m.issues[0], "bad");
        const c = st.lastCount;
        const ok = await confirmBox(`Отправить рассылку «${esc(st.m.name)}» сейчас? Получат <b>${c ? num(c.final) : "?"}</b> чел. Отменить уже доставленное будет нельзя.`, "Отправить");
        if (!ok) return;
        await api(`/mailings/${id}/send`, "POST", {});
        showMailing(id);
      } else if (action === "schedule") {
        await flushSave();
        if (st.m.issues?.length) return toast(st.m.issues[0], "bad");
        scheduleDialog();
      } else if (action === "copy" || action === "copy_new") {
        const r = await api("/mailings", "POST", { copy_of: id, exclude_received: action === "copy_new" });
        cache.mid = r.id;
        route();
      } else if (action === "delete") {
        if (!(await confirmBox("Удалить рассылку вместе со статистикой? Это нельзя отменить."))) return;
        await api(`/mailings/${id}`, "DELETE");
        cache.mid = null;
        route();
      } else {
        if (action === "cancel" && !(await confirmBox("Отменить рассылку? Ожидающие получатели не получат сообщение, уже доставленное останется."))) return;
        const path = { pause: "pause", resume: "resume", cancel: "cancel", unschedule: "unschedule", retry: "retry-failed" }[action];
        const res = await api(`/mailings/${id}/${path}`, "POST", {});
        if (action === "retry") toast(res.count ? `Повторная отправка: ${res.count} чел.` : "Неудачных доставок нет");
        showMailing(id);
      }
    } catch (err) {
      fail(err);
      if (btn) btn.disabled = false;
    }
  }

  async function flushSave() {
    if (st.saveTimer) { clearTimeout(st.saveTimer); st.saveTimer = 0; }
    await save();
  }

  function scheduleDialog() {
    const off = st.m.tz_offset_hours;
    modal(`<h3 style="margin-bottom:8px">Запланировать рассылку</h3><p class="km-muted">Время указывается по часовому поясу UTC${off >= 0 ? "+" : ""}${off} (меняется во вкладке «Настройки»). Получатели определяются в момент отправки.</p>
      <input type="datetime-local" data-at value="${esc(st.m.scheduled_local || localInputValue(off))}" style="width:100%"><div data-res></div>
      <div class="km-row" style="justify-content:flex-end;margin-top:12px"><button class="km-btn" data-no>Отмена</button><button class="km-btn km-primary" data-go>Запланировать</button></div>`, (m, close) => {
      m.querySelector("[data-no]").onclick = close;
      m.querySelector("[data-go]").onclick = async () => {
        try {
          await api(`/mailings/${st.m.id}/schedule`, "POST", { at: m.querySelector("[data-at]").value });
          close();
          toast("Рассылка запланирована");
          showMailing(st.m.id);
        } catch (err) { m.querySelector("[data-res]").innerHTML = `<div class="km-msg" style="margin-top:8px">${esc(err.message)}</div>`; }
      };
    });
  }

  // ------------------------------------------------------------------ composer
  function renderComposer(page) {
    const meta = st.meta;
    page.innerHTML = `<div class="km-compose"><div style="display:flex;flex-direction:column;gap:14px;min-width:0">
        <div class="km-card" data-aud><h3>1. Получатели</h3></div>
        <div class="km-card" data-msg><h3>2. Сообщение</h3></div>
        <div class="km-card" data-poll><h3>3. Опрос <label class="chk" style="font-weight:400"><input type="checkbox" data-poll-on> добавить опрос</label></h3><div data-poll-body></div></div>
        <div class="km-card" data-opts><h3>4. Параметры отправки</h3></div></div>
      <div class="km-side"><div class="km-card"><div class="km-muted">Получат</div><div class="km-count" data-count>…</div><div data-count-info class="km-muted" style="margin-top:6px"></div></div>
        <div class="km-card"><h3>Предпросмотр</h3><div class="tg" data-preview></div></div><div data-issues></div></div></div>`;
    renderAudience(page.querySelector("[data-aud]"));
    renderMessage(page.querySelector("[data-msg]"));
    renderPoll(page.querySelector("[data-poll]"));
    renderOptions(page.querySelector("[data-opts]"));
    paintPreview();
    paintIssues();
    refreshCount();
  }

  const toggle = (list, value) => { const i = list.indexOf(value); if (i >= 0) list.splice(i, 1); else list.push(value); };

  function renderAudience(card) {
    const a = st.m.audience;
    const meta = st.meta;
    const chips = (items, selected, key, label = (x) => x) => items.map((x) => `<button type="button" class="km-chip ${selected.includes(x.id ?? x) ? "on" : ""}" data-chip="${key}" data-v="${esc(x.id ?? x)}">${esc(label(x))}</button>`).join("");
    const exclude = a.exclude;
    card.innerHTML = `<h3>1. Получатели</h3>
      <div class="km-muted" style="margin-bottom:6px">Группы объединяются: человек получит рассылку, если он входит хотя бы в одну.</div>
      <div class="km-chips">${chips(meta.groups, a.groups, "groups", (g) => g.label)}</div>
      ${meta.tariffs.length ? `<div class="km-muted" style="margin:10px 0 6px">Тарифы</div><div class="km-chips">${chips(meta.tariffs, a.tariffs, "tariffs")}</div>` : ""}
      ${meta.core_groups.length ? `<div class="km-muted" style="margin:10px 0 6px">Группы Minishop и плагинов</div><div class="km-chips">${chips(meta.core_groups.map((g) => ({ id: g.target, label: g.label })), a.core_groups, "core_groups", (g) => g.label)}</div>` : ""}
      ${meta.lists.length ? `<div class="km-muted" style="margin:10px 0 6px">Сохранённые списки</div><div class="km-chips">${meta.lists.map((l) => `<button type="button" class="km-chip ${a.lists.includes(l.id) ? "on" : ""}" data-chip="lists" data-v="${l.id}">${esc(l.name)} · ${num(l.n)}</button>`).join("")}</div>` : ""}
      <details class="km-sub" ${Object.keys(a.filter).length ? "open" : ""}><summary>Свои условия</summary><div class="km-form" data-filter style="margin-top:10px"></div></details>
      <div class="km-sub"><div class="km-muted" style="margin-bottom:6px">Добавить вручную: ID, Telegram ID или @username через пробел, запятую или с новой строки</div>
        <textarea data-manual rows="3" placeholder="123456789, @username, 987654321">${esc(a.manual)}</textarea>
        <div class="km-row" style="margin-top:6px"><button type="button" class="km-btn" data-check>Проверить</button><button type="button" class="km-btn" data-savelist>Сохранить как список</button><span class="km-muted" data-manual-res></span></div></div>
      <details class="km-sub" ${exclude.manual || exclude.lists.length || exclude.mailings.length ? "open" : ""}><summary>Исключить</summary>
        <div class="km-muted" style="margin:10px 0 6px">Не отправлять этим людям (ID, Telegram ID или @username)</div><textarea data-ex-manual rows="2">${esc(exclude.manual)}</textarea>
        ${meta.lists.length ? `<div class="km-muted" style="margin:10px 0 6px">Списки</div><div class="km-chips">${meta.lists.map((l) => `<button type="button" class="km-chip ${exclude.lists.includes(l.id) ? "on" : ""}" data-chip="ex-lists" data-v="${l.id}">${esc(l.name)}</button>`).join("")}</div>` : ""}
        ${meta.recent_mailings.length ? `<div class="km-muted" style="margin:10px 0 6px">Тем, кто уже получил рассылку</div><div class="km-chips">${meta.recent_mailings.filter((x) => x.id !== st.m.id).map((x) => `<button type="button" class="km-chip ${exclude.mailings.includes(x.id) ? "on" : ""}" data-chip="ex-mailings" data-v="${x.id}">${esc(x.name)}</button>`).join("")}</div>` : ""}</details>`;
    card.querySelectorAll("[data-chip]").forEach((b) => b.addEventListener("click", () => {
      const key = b.dataset.chip, v = b.dataset.v;
      const num_ = ["lists", "ex-lists", "ex-mailings"].includes(key) ? Number(v) : v;
      toggle({ groups: a.groups, tariffs: a.tariffs, core_groups: a.core_groups, lists: a.lists, "ex-lists": exclude.lists, "ex-mailings": exclude.mailings }[key], num_);
      b.classList.toggle("on");
      changed();
    }));
    // custom filter form
    const form = card.querySelector("[data-filter]");
    form.innerHTML = meta.filter_fields.map((f) => {
      const v = a.filter[f.key];
      if (f.type === "select") return `<label class="f">${esc(f.label)}<select data-fk="${f.key}">${f.options.map((o) => `<option value="${o.value}" ${(v ?? f.default) === o.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select></label>`;
      if (f.type === "tags") return `<label class="f">${esc(f.label)}<input type="text" data-fk="${f.key}" value="${esc((v || []).join(", "))}"></label>`;
      return `<label class="f">${esc(f.label)}<input type="number" min="${f.min}" max="${f.max}" data-fk="${f.key}" value="${esc(v ?? "")}"></label>`;
    }).join("");
    form.querySelectorAll("[data-fk]").forEach((c) => c.addEventListener("input", () => {
      const out = {};
      form.querySelectorAll("[data-fk]").forEach((x) => {
        const f = meta.filter_fields.find((y) => y.key === x.dataset.fk);
        const raw = x.value.trim();
        if (f.type === "select") { if (raw && raw !== "any") out[f.key] = raw; }
        else if (f.type === "tags") { if (raw) out[f.key] = raw.split(",").map((t) => t.trim()).filter(Boolean); }
        else if (raw !== "") out[f.key] = Number(raw);
      });
      a.filter = out;
      changed();
    }));
    card.querySelector("[data-manual]").addEventListener("input", (e) => { a.manual = e.target.value; changed(); });
    card.querySelector("[data-ex-manual]").addEventListener("input", (e) => { exclude.manual = e.target.value; changed(); });
    card.querySelector("[data-check]").onclick = async () => {
      try {
        const r = await api("/manual", "POST", { text: a.manual });
        card.querySelector("[data-manual-res]").innerHTML = `Найдено: <b>${r.found}</b>${r.unresolved.length ? ` · не найдено: <span style="color:#e5484d">${esc(r.unresolved.slice(0, 8).join(", "))}${r.unresolved.length > 8 ? "…" : ""}</span>` : ""}`;
      } catch (err) { fail(err); }
    };
    card.querySelector("[data-savelist]").onclick = () => {
      modal(`<h3 style="margin-bottom:8px">Сохранить как список</h3><input type="text" data-n maxlength="80" placeholder="Название списка" style="width:100%"><div class="km-row" style="justify-content:flex-end;margin-top:12px"><button class="km-btn" data-no>Отмена</button><button class="km-btn km-primary" data-go>Сохранить</button></div>`, (m, close) => {
        m.querySelector("[data-no]").onclick = close;
        m.querySelector("[data-go]").onclick = async () => {
          try {
            const r = await api("/lists", "POST", { name: m.querySelector("[data-n]").value, text: a.manual });
            close();
            toast(`Список сохранён: ${r.count} чел.`);
            await loadMeta(true);
            a.lists.push(r.id);
            a.manual = "";
            renderAudience(card);
            changed();
          } catch (err) { fail(err); }
        };
      });
    };
  }

  function renderMessage(card) {
    const c = st.m.content;
    const meta = st.meta;
    card.innerHTML = `<h3>2. Сообщение</h3>
      <div class="km-row" style="margin-bottom:6px">${[["b", "<b>Ж</b>"], ["i", "<i>К</i>"], ["u", "<u>П</u>"], ["s", "<s>З</s>"], ["code", "{ }"], ["a", "🔗"]].map(([t, l]) => `<button type="button" class="km-btn" data-tag="${t}" style="padding:4px 10px">${l}</button>`).join("")}<span class="km-grow"></span><span class="km-muted" data-count-text></span></div>
      <textarea data-text rows="7" placeholder="Текст сообщения. Поддерживается разметка Telegram: &lt;b&gt;, &lt;i&gt;, &lt;a href=...&gt;, &lt;code&gt;">${esc(c.text)}</textarea>
      <div class="km-muted" style="margin-top:4px" data-caption-hint></div>
      <div class="km-sub"><div class="km-row"><b>Медиа</b><span class="km-muted">фото, видео, GIF, файлы (до ${meta.limits.media} штук; альбом из нескольких)</span><span class="km-grow"></span>
        <label class="km-btn" style="cursor:pointer">+ Добавить файл<input type="file" multiple hidden data-file></label></div><div data-media></div></div>
      <div class="km-sub"><div class="km-row"><b>Кнопки</b><span class="km-muted">до 4, под сообщением</span></div><div data-buttons></div></div>`;
    const ta = card.querySelector("[data-text]");
    const refresh = () => {
      card.querySelector("[data-count-text]").textContent = `${ta.value.length}/${meta.limits.text}`;
      card.querySelector("[data-caption-hint]").textContent = c.media.length && ta.value.length > meta.limits.caption
        ? `Текст длиннее ${meta.limits.caption} знаков, поэтому он уйдёт отдельным сообщением перед медиа (кнопки останутся на тексте).` : "";
    };
    ta.addEventListener("input", () => { c.text = ta.value; refresh(); changed(false); });
    refresh();
    card.querySelectorAll("[data-tag]").forEach((b) => b.addEventListener("click", () => {
      const tag = b.dataset.tag;
      const s = ta.selectionStart, e = ta.selectionEnd;
      const sel = ta.value.slice(s, e) || "текст";
      let open = `<${tag}>`, close = `</${tag}>`;
      if (tag === "a") { const url = prompt("Адрес ссылки (https://…)"); if (!url) return; open = `<a href="${url}">`; }
      ta.value = ta.value.slice(0, s) + open + sel + close + ta.value.slice(e);
      ta.focus();
      ta.selectionStart = s + open.length;
      ta.selectionEnd = s + open.length + sel.length;
      c.text = ta.value;
      refresh();
      changed(false);
    }));
    const drawMedia = () => {
      const box = card.querySelector("[data-media]");
      box.innerHTML = c.media.map((x, i) => {
        const info = st.files[x.id] || {};
        const preview = x.kind === "photo" ? `<img src="${API}/media/${x.id}" alt="">` : `<div class="ph">${x.kind === "video" ? "🎬" : x.kind === "animation" ? "🎞" : "📄"}</div>`;
        return `<div class="km-media" data-i="${i}">${preview}<div class="km-grow"><b>${MEDIA_RU[x.kind]}</b> <span class="km-muted">${esc(info.name || "")} ${info.size ? fmtSize(info.size) : ""}</span>
          ${x.kind === "video" || x.kind === "animation" ? `<label class="chk" style="margin-top:4px"><input type="checkbox" data-gif ${x.kind === "animation" ? "checked" : ""}> отправить как GIF (без звука, зацикленно)</label>` : ""}</div>
          <button class="km-btn" data-up ${i === 0 ? "disabled" : ""}>↑</button><button class="km-btn" data-dn ${i === c.media.length - 1 ? "disabled" : ""}>↓</button><button class="km-btn km-danger" data-rm>✕</button></div>`;
      }).join("") + `<div data-uploads></div>`;
      box.querySelectorAll("[data-i]").forEach((row) => {
        const i = Number(row.dataset.i);
        row.querySelector("[data-rm]").onclick = () => { c.media.splice(i, 1); drawMedia(); refresh(); changed(false); };
        row.querySelector("[data-up]").onclick = () => { [c.media[i - 1], c.media[i]] = [c.media[i], c.media[i - 1]]; drawMedia(); changed(false); };
        row.querySelector("[data-dn]").onclick = () => { [c.media[i + 1], c.media[i]] = [c.media[i], c.media[i + 1]]; drawMedia(); changed(false); };
        row.querySelector("[data-gif]")?.addEventListener("change", (e) => { c.media[i].kind = e.target.checked ? "animation" : "video"; drawMedia(); changed(false); });
      });
    };
    drawMedia();
    card.querySelector("[data-file]").addEventListener("change", async (e) => {
      const files = [...e.target.files];
      e.target.value = "";
      for (const file of files) {
        if (c.media.length >= meta.limits.media) { toast(`Не больше ${meta.limits.media} файлов`, "bad"); break; }
        const row = h(`<div class="km-media"><div class="ph">⏳</div><div class="km-grow">${esc(file.name)}<div class="km-prog"><i style="width:0%"></i></div></div></div>`);
        card.querySelector("[data-uploads]").appendChild(row);
        try {
          const res = await uploadFile(file, meta, (p) => { row.querySelector("i").style.width = `${Math.round(p * 100)}%`; });
          st.files[res.id] = { name: res.name, size: res.size };
          c.media.push({ id: res.id, kind: res.kind });
          drawMedia();
          refresh();
          changed(false);
        } catch (err) { row.remove(); fail(err); }
      }
    });
    const drawButtons = () => {
      const box = card.querySelector("[data-buttons]");
      box.innerHTML = c.buttons.map((b, i) => `<div class="km-media" data-b="${i}" style="flex-wrap:wrap">
        <select data-k><option value="url" ${b.kind === "url" ? "selected" : ""}>Ссылка</option><option value="webapp_section" ${b.kind === "webapp_section" ? "selected" : ""}>Раздел Mini App</option></select>
        <input type="text" data-label maxlength="64" placeholder="Подпись" value="${esc(b.label)}" style="flex:1;min-width:120px">
        ${b.kind === "url" ? `<input type="text" data-url placeholder="https://…" value="${esc(b.url)}" style="flex:2;min-width:160px">` : `<select data-sec>${meta.sections.map((s) => `<option value="${s}" ${b.section === s ? "selected" : ""}>${SECTIONS_RU[s] || s}</option>`).join("")}</select>`}
        <button class="km-btn km-danger" data-rm>✕</button></div>`).join("") + (c.buttons.length < 4 ? `<button class="km-btn" data-addb style="margin-top:8px">+ Кнопка</button>` : "");
      box.querySelectorAll("[data-b]").forEach((row) => {
        const i = Number(row.dataset.b), b = c.buttons[i];
        row.querySelector("[data-k]").onchange = (e) => { b.kind = e.target.value; drawButtons(); changed(false); };
        row.querySelector("[data-label]").oninput = (e) => { b.label = e.target.value; changed(false); };
        row.querySelector("[data-url]")?.addEventListener("input", (e) => { b.url = e.target.value; changed(false); });
        row.querySelector("[data-sec]")?.addEventListener("change", (e) => { b.section = e.target.value; changed(false); });
        row.querySelector("[data-rm]").onclick = () => { c.buttons.splice(i, 1); drawButtons(); changed(false); };
      });
      box.querySelector("[data-addb]")?.addEventListener("click", () => { c.buttons.push({ kind: "url", label: "", url: "", section: "home" }); drawButtons(); changed(false); });
    };
    drawButtons();
  }

  function renderPoll(card) {
    const c = st.m.content;
    const meta = st.meta;
    const on = card.querySelector("[data-poll-on]");
    on.checked = !!c.poll;
    const draw = () => {
      const box = card.querySelector("[data-poll-body]");
      if (!c.poll) { box.innerHTML = `<p class="km-muted" style="margin:0">Опрос уйдёт отдельным сообщением после текста. Ответы каждого человека видны в статистике: вы увидите, кому отправили и что он выбрал.</p>`; return; }
      const p = c.poll;
      box.innerHTML = `<label class="f">Вопрос<input type="text" data-q maxlength="${meta.limits.question}" value="${esc(p.question)}"></label>
        <div class="km-muted" style="margin:10px 0 4px">Варианты ответа (${meta.limits.poll_options[0]}–${meta.limits.poll_options[1]})</div>
        ${p.options.map((o, i) => `<div class="km-row" style="margin-bottom:6px" data-o="${i}">${p.quiz ? `<input type="radio" name="correct" data-c ${p.correct === i ? "checked" : ""} title="Правильный ответ">` : ""}<input type="text" data-ot maxlength="${meta.limits.option}" value="${esc(o)}" style="flex:1" placeholder="Вариант ${i + 1}"><button class="km-btn km-danger" data-orm ${p.options.length <= meta.limits.poll_options[0] ? "disabled" : ""}>✕</button></div>`).join("")}
        ${p.options.length < meta.limits.poll_options[1] ? `<button class="km-btn" data-oadd>+ Вариант</button>` : ""}
        <div class="km-sub"><label class="chk"><input type="checkbox" data-quiz ${p.quiz ? "checked" : ""}> Викторина (один правильный ответ)</label>
          ${p.quiz ? `<label class="f" style="margin-top:8px">Пояснение после ответа (до ${meta.limits.explanation} знаков)<input type="text" data-expl maxlength="${meta.limits.explanation}" value="${esc(p.explanation)}"></label>` : `<label class="chk" style="margin-top:8px"><input type="checkbox" data-multi ${p.multiple ? "checked" : ""}> Можно выбрать несколько вариантов</label>`}
          <label class="f" style="margin-top:8px">Закрыть опрос через, часов (0 — не закрывать)<input type="number" min="0" max="720" data-close value="${p.close_hours}" style="width:120px"></label></div>`;
      box.querySelector("[data-q]").oninput = (e) => { p.question = e.target.value; changed(false); };
      box.querySelectorAll("[data-o]").forEach((row) => {
        const i = Number(row.dataset.o);
        row.querySelector("[data-ot]").oninput = (e) => { p.options[i] = e.target.value; changed(false); };
        row.querySelector("[data-orm]").onclick = () => { p.options.splice(i, 1); if (p.correct >= p.options.length) p.correct = 0; draw(); changed(false); };
        row.querySelector("[data-c]")?.addEventListener("change", () => { p.correct = i; changed(false); });
      });
      box.querySelector("[data-oadd]")?.addEventListener("click", () => { p.options.push(""); draw(); changed(false); });
      box.querySelector("[data-quiz]").onchange = (e) => { p.quiz = e.target.checked; if (p.quiz) p.multiple = false; draw(); changed(false); };
      box.querySelector("[data-expl]")?.addEventListener("input", (e) => { p.explanation = e.target.value; changed(false); });
      box.querySelector("[data-multi]")?.addEventListener("change", (e) => { p.multiple = e.target.checked; changed(false); });
      box.querySelector("[data-close]").oninput = (e) => { p.close_hours = Math.max(0, Math.min(720, Number(e.target.value) || 0)); changed(false); };
    };
    on.onchange = () => {
      c.poll = on.checked ? { question: "", options: ["", ""], quiz: false, multiple: false, correct: 0, explanation: "", close_hours: 0 } : null;
      draw();
      changed(false);
    };
    draw();
  }

  function renderOptions(card) {
    const c = st.m.content;
    card.innerHTML = `<label class="chk"><input type="checkbox" data-prev ${c.disable_preview ? "" : "checked"}> Показывать превью ссылок</label>
      <label class="chk" style="margin-top:8px"><input type="checkbox" data-silent ${c.silent ? "checked" : ""}> Тихая отправка (без звука уведомления)</label>
      <label class="chk" style="margin-top:8px"><input type="checkbox" data-protect ${c.protect ? "checked" : ""}> Запретить пересылку и сохранение</label>`;
    card.querySelector("[data-prev]").onchange = (e) => { c.disable_preview = !e.target.checked; changed(false); };
    card.querySelector("[data-silent]").onchange = (e) => { c.silent = e.target.checked; changed(false); };
    card.querySelector("[data-protect]").onchange = (e) => { c.protect = e.target.checked; changed(false); };
  }

  // ------------------------------------------------------------------ side panels
  const refreshCount = debounce(async () => {
    const seq = ++st.previewSeq;
    const countEl = body().querySelector("[data-count]");
    if (!countEl || !st.m) return;
    countEl.style.opacity = ".5";
    try {
      const { preview } = await api(`/mailings/${st.m.id}/audience`, "POST", { audience: st.m.audience });
      if (seq !== st.previewSeq || !body().querySelector("[data-count]")) return;
      st.lastCount = preview;
      const i = preview.ineligible;
      const lines = [`Выбрано: <b>${num(preview.selected)}</b>`];
      if (preview.excluded) lines.push(`Исключено вручную: <b>${num(preview.excluded)}</b>`);
      const drop = [];
      if (i.banned) drop.push(`забанены: ${num(i.banned)}`);
      if (i.no_telegram) drop.push(`нет Telegram: ${num(i.no_telegram)}`);
      if (i.blocked) drop.push(`заблокировали бота: ${num(i.blocked)}`);
      if (i.missing) drop.push(`не найдены: ${num(i.missing)}`);
      if (drop.length) lines.push(`Не получат (${drop.join(", ")})`);
      if (preview.unresolved.length) lines.push(`<span style="color:#e5484d">Не найдено в списке: ${esc(preview.unresolved.slice(0, 5).join(", "))}${preview.unresolved.length > 5 ? "…" : ""}</span>`);
      if (preview.sample.length) lines.push(`Например: ${preview.sample.map((s) => esc(s.username ? "@" + s.username : s.first_name || s.user_id)).join(", ")}`);
      countEl.textContent = num(preview.final);
      body().querySelector("[data-count-info]").innerHTML = lines.join("<br>");
    } catch (err) {
      countEl.textContent = "—";
    }
    countEl.style.opacity = "1";
  }, 700);

  function paintPreview() {
    const box = body().querySelector("[data-preview]");
    if (!box || !st.m) return;
    const c = st.m.content;
    const kb = c.buttons.filter((b) => b.label || b.kind === "webapp_section");
    const kbHtml = kb.length ? `<div class="tg-kb">${kb.map((b) => `<div>${esc(b.label || SECTIONS_RU[b.section] || "Открыть")}</div>`).join("")}</div>` : "";
    const mediaHtml = c.media.length ? `<div class="tg-media">${c.media.slice(0, 1).map((x) => x.kind === "photo" ? `<img src="${API}/media/${x.id}" alt="">` : `<div class="ph">${x.kind === "video" ? "🎬 видео" : x.kind === "animation" ? "🎞 GIF" : "📄 файл"}</div>`).join("")}${c.media.length > 1 ? `<div class="ph">+ ещё ${c.media.length - 1} в альбоме</div>` : ""}</div>` : "";
    const longText = c.media.length && c.text.length > 1024;
    let html = "";
    const textHtml = c.text ? safeHtml(c.text) : "";
    if (longText) html += `<div class="tg-bubble">${textHtml}${kbHtml}</div><div class="tg-bubble">${mediaHtml}</div>`;
    else if (c.media.length || c.text) html += `<div class="tg-bubble">${mediaHtml}${textHtml}${c.media.length > 1 ? "" : kbHtml}</div>`;
    if (c.poll) {
      const p = c.poll;
      html += `<div class="tg-bubble tg-poll"><b>${esc(p.question || "Вопрос опроса")}</b><div class="km-muted" style="margin-bottom:6px;color:#8aa0b5">${p.quiz ? "Викторина" : "Опрос"}${p.multiple ? " · несколько ответов" : ""}</div>${p.options.map((o) => `<div class="o"><i class="${p.multiple ? "sq" : ""}"></i>${esc(o || "Вариант")}</div>`).join("")}</div>`;
    }
    box.innerHTML = html || `<div class="km-muted">Здесь появится предпросмотр</div>`;
  }

  function paintIssues() {
    const box = body().querySelector("[data-issues]");
    if (!box || !st.m) return;
    const issues = st.m.issues || [];
    box.innerHTML = issues.length
      ? `<div class="km-banner bad">${issues.map((i) => `<div>● ${esc(i)}</div>`).join("")}</div>`
      : `<div class="km-banner ok">Рассылка готова к отправке.</div>`;
  }

  // ------------------------------------------------------------------ stats (non-editable statuses)
  async function renderStats(page) {
    const m = st.m;
    page.innerHTML = `<div data-stats class="km-muted">Загрузка…</div>`;
    st.logState = { status: "", q: "", offset: 0 };
    const paint = async () => {
      if (!body().querySelector("[data-stats]")) return;
      const { stats: s } = await api(`/mailings/${m.id}/stats`);
      const c = s.counts;
      const seg = (n, color) => (c.total ? `<i style="width:${(n / c.total) * 100}%;background:${color}"></i>` : "");
      const poll = s.poll;
      body().querySelector("[data-stats]").innerHTML = `${m.last_error ? `<div class="km-banner bad">${esc(m.last_error)}</div>` : ""}
        <div class="km-grid"><div class="km-stat"><span class="km-muted">Получателей</span><b>${num(c.total)}</b></div>
          <div class="km-stat"><span class="km-muted">Доставлено</span><b style="color:#22c55e">${num(c.sent)}</b><span class="km-muted">${pct(s.delivered_pct)}</span></div>
          <div class="km-stat"><span class="km-muted">Ожидают</span><b>${num(c.pending)}</b></div>
          <div class="km-stat"><span class="km-muted">Ошибки</span><b style="color:#e5484d">${num(c.failed)}</b></div>
          <div class="km-stat"><span class="km-muted">Заблокировали бота</span><b style="color:#f59e0b">${num(c.blocked)}</b></div>
          <div class="km-stat"><span class="km-muted">Пропущено</span><b>${num(c.skipped)}</b></div></div>
        <div class="km-card"><div class="km-bar">${seg(c.sent, "#22c55e")}${seg(c.failed, "#e5484d")}${seg(c.blocked, "#f59e0b")}${seg(c.skipped, "#6b7280")}</div>
          <div class="km-row km-muted" style="margin-top:8px">Старт: ${fmtDate(s.started_at)}<span>·</span>${s.finished_at ? `Завершена: ${fmtDate(s.finished_at)}` : s.eta_seconds !== null ? `Осталось примерно: ${fmtDur(s.eta_seconds)}` : ""}<span>·</span>Скорость: ${s.speed_per_sec ? `${s.speed_per_sec}/с` : "—"}</div></div>
        ${s.errors.length ? `<div class="km-card"><h3>Причины недоставки</h3><table class="km-table">${s.errors.map((e) => `<tr><td>${esc(RSTATUS[e.status] || e.status)}</td><td>${esc(e.error)}</td><td>${num(e.count)}</td></tr>`).join("")}</table></div>` : ""}
        ${poll ? `<div class="km-card"><h3>Опрос: ${esc(poll.question)}</h3><div class="km-muted" style="margin-bottom:8px">Ответили ${num(poll.voters)} из ${num(poll.polls_sent)} (${pct(poll.response_pct)})${poll.quiz && poll.correct_pct !== undefined ? ` · правильно ответили ${pct(poll.correct_pct)}` : ""}</div>
          ${poll.options.map((o) => `<div style="margin-bottom:8px"><div class="km-row"><span class="km-grow">${esc(o.text)}${o.correct ? " ✓" : ""}</span><b>${num(o.votes)}</b><span class="km-muted">${pct(o.pct)}</span></div><div class="km-prog"><i style="width:${o.pct}%"></i></div></div>`).join("")}</div>` : ""}
        <div class="km-card"><h3>Содержимое</h3><div class="tg" data-preview></div></div>
        <div class="km-card"><h3>Получатели <span class="km-row"><button class="km-btn" data-export>Скачать CSV</button></span></h3>
          <div class="km-row" style="margin-bottom:8px"><select data-st><option value="">Все статусы</option>${Object.entries(RSTATUS).map(([k, t]) => `<option value="${k}" ${st.logState.status === k ? "selected" : ""}>${t}</option>`).join("")}</select>
            <input type="text" data-q placeholder="ID или @username" value="${esc(st.logState.q)}" style="width:180px"><button class="km-btn" data-find>Найти</button></div>
          <div class="km-scroll" data-log></div></div>`;
      paintPreview();
      body().querySelector("[data-export]").onclick = async () => {
        try {
          const { rows } = await api(`/mailings/${m.id}/export`);
          const names = (poll ? poll.options.map((o) => o.text) : []);
          downloadCsv(`mailing-${m.id}.csv`, [["ID", "Username", "Статус", "Ошибка", "Время", ...(poll ? ["Ответ"] : [])], ...rows.map((r) => [r.user_id, r.username || "", RSTATUS[r.status] || r.status, r.error || "", r.sent_at || "", ...(poll ? [(r.option_ids || []).map((i) => names[i]).join(" | ")] : [])])]);
        } catch (err) { fail(err); }
      };
      body().querySelector("[data-find]").onclick = () => { st.logState.status = body().querySelector("[data-st]").value; st.logState.q = body().querySelector("[data-q]").value; st.logState.offset = 0; loadLog(poll); };
      body().querySelector("[data-st]").onchange = () => body().querySelector("[data-find]").click();
      await loadLog(poll);
    };
    await paint().catch(fail);
    if (m.status === "sending") st.pollTimer = setInterval(() => { if (!st.disposed && body().querySelector("[data-stats]")) paintLiveCounters(); }, 4000);
  }

  async function paintLiveCounters() {
    try {
      const { stats: s } = await api(`/mailings/${st.m.id}/stats`);
      if (s.status !== "sending") { clearInterval(st.pollTimer); showMailing(st.m.id); return; }
      const c = s.counts;
      const cards = body().querySelectorAll(".km-stat b");
      if (cards.length >= 6) [c.total, c.sent, c.pending, c.failed, c.blocked, c.skipped].forEach((v, i) => (cards[i].textContent = num(v)));
      const bar = body().querySelector(".km-bar");
      if (bar && c.total) bar.innerHTML = [[c.sent, "#22c55e"], [c.failed, "#e5484d"], [c.blocked, "#f59e0b"], [c.skipped, "#6b7280"]].map(([n, col]) => `<i style="width:${(n / c.total) * 100}%;background:${col}"></i>`).join("");
    } catch { /* next tick */ }
  }

  async function loadLog(poll) {
    const { status, q, offset } = st.logState;
    const box = body().querySelector("[data-log]");
    if (!box) return;
    const page = await api(`/mailings/${st.m.id}/recipients?limit=50&offset=${offset}&status=${encodeURIComponent(status)}&q=${encodeURIComponent(q)}`);
    const names = poll ? poll.options.map((o) => o.text) : [];
    box.innerHTML = `<table class="km-table"><tr><th>Пользователь</th><th>Статус</th><th>Время</th><th>Ошибка</th>${poll ? "<th>Ответ</th>" : ""}</tr>
      ${page.rows.map((r) => `<tr><td>${r.user_id}${r.username ? ` <span class="km-muted">@${esc(r.username)}</span>` : ""}</td><td>${esc(RSTATUS[r.status] || r.status)}</td><td>${fmtDate(r.sent_at)}</td><td class="km-muted">${esc(r.error || "")}</td>${poll ? `<td>${esc((r.option_ids || []).map((i) => names[i]).join(", ") || "—")}</td>` : ""}</tr>`).join("") || `<tr><td colspan="5" class="km-muted">Нет записей</td></tr>`}</table>
      <div class="km-row" style="margin-top:8px"><button class="km-btn" data-prev ${offset ? "" : "disabled"}>←</button><span class="km-muted">${offset + 1}–${offset + page.rows.length} из ${num(page.total)}</span><button class="km-btn" data-next ${offset + 50 < page.total ? "" : "disabled"}>→</button></div>`;
    box.querySelector("[data-prev]").onclick = () => { st.logState.offset = Math.max(0, offset - 50); loadLog(poll).catch(fail); };
    box.querySelector("[data-next]").onclick = () => { st.logState.offset = offset + 50; loadLog(poll).catch(fail); };
  }

  // ------------------------------------------------------------------ saved lists
  async function showLists() {
    const meta = await loadMeta(true);
    shell(`<div class="km-row"><button class="km-btn km-primary" data-new>+ Новый список</button><span class="km-muted">Списки можно добавлять в получатели любой рассылки или исключать из неё.</span></div>
      <div class="km-card km-scroll">${meta.lists.length ? `<table class="km-table"><tr><th>Название</th><th>Человек</th><th></th></tr>${meta.lists.map((l) => `<tr><td><b>${esc(l.name)}</b></td><td>${num(l.n)}</td><td class="km-row"><button class="km-btn" data-edit="${l.id}">Изменить</button><button class="km-btn km-danger" data-del="${l.id}">Удалить</button></td></tr>`).join("")}</table>` : `<p class="km-muted" style="margin:0">Списков пока нет.</p>`}</div>`);
    const edit = async (id) => {
      let current = { name: "", text: "" };
      if (id) current = (await api(`/lists/${id}`)).list;
      modal(`<h3 style="margin-bottom:8px">${id ? "Изменить список" : "Новый список"}</h3><input type="text" data-n maxlength="80" placeholder="Название" value="${esc(current.name)}" style="width:100%;margin-bottom:8px">
        <div class="km-muted" style="margin-bottom:4px">ID, Telegram ID или @username через пробел, запятую или с новой строки</div><textarea data-t rows="8">${esc(current.text)}</textarea><div data-res></div>
        <div class="km-row" style="justify-content:flex-end;margin-top:12px"><button class="km-btn" data-no>Отмена</button><button class="km-btn km-primary" data-go>Сохранить</button></div>`, (m, close) => {
        m.querySelector("[data-no]").onclick = close;
        m.querySelector("[data-go]").onclick = async () => {
          try {
            const payload = { name: m.querySelector("[data-n]").value, text: m.querySelector("[data-t]").value };
            const r = id ? await api(`/lists/${id}`, "PUT", payload) : await api("/lists", "POST", payload);
            close();
            toast(`Сохранено: ${r.count} чел.`);
            showLists();
          } catch (err) { m.querySelector("[data-res]").innerHTML = `<div class="km-msg" style="margin-top:8px">${esc(err.message)}</div>`; }
        };
      });
    };
    const main = body();
    main.querySelector("[data-new]").onclick = () => edit(null);
    main.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => edit(Number(b.dataset.edit)).catch(fail)));
    main.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await confirmBox("Удалить список? В рассылках, где он выбран, он перестанет учитываться."))) return;
      try { await api(`/lists/${b.dataset.del}`, "DELETE"); showLists(); } catch (e) { fail(e); }
    }));
  }

  // ------------------------------------------------------------------ settings
  async function showSettings() {
    const { settings: s } = await api("/settings");
    const f = (k, label, min, max, hint = "") => `<label class="f">${label}<input type="number" min="${min}" max="${max}" name="${k}" value="${esc(s[k])}">${hint ? `<span class="km-muted">${hint}</span>` : ""}</label>`;
    shell(`<div class="km-card"><form data-form class="km-form">
      ${f("tz_offset_hours", "Часовой пояс: смещение от UTC, ч", -12, 14, "Москва = 3. Так вы вводите время планирования")}
      ${f("rate_per_second", "Скорость отправки, сообщений в секунду", 1, 28, "Telegram допускает около 30")}
      ${f("max_attempts", "Попыток доставки на человека", 1, 10)}
      ${f("retry_minutes", "Пауза между попытками, минут", 1, 1440)}
      </form><div class="km-row" style="margin-top:12px"><button class="km-btn km-primary" data-save>Сохранить</button></div></div>`);
    body().querySelector("[data-save]").onclick = async (e) => {
      const payload = {};
      for (const el of body().querySelector("[data-form]").elements) if (el.name) payload[el.name] = Number(el.value);
      e.target.disabled = true;
      try { await api("/settings", "PUT", payload); toast("Настройки сохранены"); } catch (err) { fail(err); }
      e.target.disabled = false;
    };
  }

  route();
  return st;
}

export function mountView(view, target) {
  return mountApp(target);
}

export function updateView() {
  // Admin shell props must not re-mount the editor and wipe unsaved input.
}

export function unmountView(instance) {
  if (!instance) return;
  instance.disposed = true;
  clearTimeout(instance.saveTimer);
  clearInterval(instance.pollTimer);
  instance.root.remove();
}
