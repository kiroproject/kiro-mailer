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
