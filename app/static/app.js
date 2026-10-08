"use strict";

const MONTH_NAMES = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
  "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
const SOURCE = {
  windows: ["вход Windows / AD", "green"], os: ["учётная запись ОС", "green"],
  proxy: ["SSO через прокси", "green"],
  manual: ["введено вручную", "amber"], unknown: ["не определено", "red"],
};
const ACTION = { create: ["Создание", "green"], update: ["Изменение", "amber"], delete: ["Удаление", "red"] };
const TABS = {
  schedule: ["Раздел 01", "График дежурств"], dayoffs: ["Раздел 02", "График отгулов"],
  official: ["Раздел 03", "Официальные часы"], unofficial: ["Раздел 04", "Неофициальные часы"],
  employees: ["Раздел 05", "Дежурные"], audit: ["Раздел 06", "Журнал аудита"],
};
const KIND_BADGE = { official: "green", unofficial: "amber" };
const DUTY_KIND = { official: "Официальное", unofficial: "Неофициальное" };
const HOURS_KIND = { official: "Официальные часы", unofficial: "Неофициальные часы" };
const ROLE = { duty: "Дежурный", assistant: "Помощник дежурного", shift: "Смена", other: "Иное" };
const ROLE_SHORT = { duty: "Деж.", assistant: "Пом.", shift: "Смена", other: "Иное" };
const DEFAULT_DUTY_HOURS = 24;
const DEFAULT_DAYOFF_HOURS = 8;

const $ = (sel) => document.querySelector(sel);
const view = $("#view");
const state = { tab: "schedule", month: currentMonth(), employees: [], auditOffset: 0 };

function pad(n) { return String(n).padStart(2, "0"); }
function currentMonth() { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`; }
function todayStr() { const d = new Date(); return `${currentMonth()}-${pad(d.getDate())}`; }
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtNum(n) { return Number(n).toLocaleString("ru-RU", { maximumFractionDigits: 2 }); }
function fmtSigned(n) { return (n > 0 ? "+" : n < 0 ? "−" : "") + fmtNum(Math.abs(n)); }
function options(map, selected) {
  return Object.entries(map).map(([v, t]) => `<option value="${v}" ${v === selected ? "selected" : ""}>${esc(t)}</option>`).join("");
}
function monthTitle(m) { const [y, mo] = m.split("-").map(Number); return `${MONTH_NAMES[mo - 1]} ${y}`; }
function shiftMonth(m, delta) {
  const [y, mo] = m.split("-").map(Number);
  const d = new Date(y, mo - 1 + delta, 1);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}
function plural(n, forms) {
  const a = Math.abs(n) % 100, b = a % 10;
  return forms[a > 10 && a < 20 ? 2 : b > 1 && b < 5 ? 1 : b === 1 ? 0 : 2];
}
function badge(text, color = "", plain = false) {
  return `<span class="badge ${color} ${plain ? "plain" : ""}">${esc(text)}</span>`;
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Ошибка ${res.status}`);
  return data;
}

let toastTimer;
function toast(message, ok = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (ok ? " ok" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 4000);
}

/** Выполняет действие и показывает ошибку, если оно не удалось. */
async function guarded(fn) {
  try { await fn(); return true; } catch (err) { toast(err.message); return false; }
}

// ---------- Диалог ----------
function openForm(title, bodyHtml, onSubmit, submitLabel = "Сохранить") {
  const dlg = $("#dialog");
  dlg.innerHTML = `<h3>${esc(title)}</h3><form method="dialog">${bodyHtml}
    <div class="actions"><button type="button" class="btn" id="dlg-cancel">Отмена</button>
    <button type="submit" class="btn approve">${esc(submitLabel)}</button></div></form>`;
  const form = dlg.querySelector("form");
  dlg.querySelector("#dlg-cancel").onclick = () => dlg.close();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    if (await guarded(() => onSubmit(data))) dlg.close();
  };
  dlg.showModal();
  const first = form.querySelector("input, select");
  if (first) first.focus();
}

/** Варианты выбора сотрудника. `current` — сотрудник редактируемой записи: если он удалён из списка,
 *  его всё равно нужно показать, иначе форма молча подставит другого человека. */
function employeeOptions(selectedId, current) {
  const list = [...state.employees];
  if (current && !list.some((e) => e.id === current.employee_id)) {
    list.unshift({ id: current.employee_id, full_name: current.full_name, position: `${current.position} (удалён)` });
  }
  return list.map((e) =>
    `<option value="${e.id}" ${e.id === selectedId ? "selected" : ""}>${esc(e.full_name)} — ${esc(e.position)}</option>`).join("");
}

/** Карточка: заголовок, необязательный бейдж справа, тело. */
function card(title, body, { right = "", flush = false } = {}) {
  return `<div class="card"><div class="card-head"><h2>${esc(title)}</h2>${right}</div>
    <div class="card-body ${flush ? "flush" : ""}">${body}</div></div>`;
}

// ---------- Пользователь ----------
async function loginWindows() {
  // Браузер сам ответит на запрос Negotiate учёткой, под которой выполнен вход в Windows.
  // Если сайт не в зоне «Местная интрасеть», браузер может спросить логин и пароль домена.
  const res = await fetch("/api/login/windows", { credentials: "same-origin", cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Вход Windows не выполнен (${res.status})`);
  return data;
}

async function loadUser({ tryWindows = true } = {}) {
  let me = await api("GET", "/api/me");
  let tried = false;
  try { tried = sessionStorage.getItem("perer_win_tried") === "1"; } catch (_) { /* приватный режим */ }
  if (tryWindows && me.windows_auth && !tried && (me.source === "unknown" || me.source === "manual")) {
    try { sessionStorage.setItem("perer_win_tried", "1"); } catch (_) { /* приватный режим */ }
    try {
      await loginWindows();
      me = await api("GET", "/api/me");
    } catch (err) {
      console.warn(err);
    }
  }
  const [label, color] = SOURCE[me.source] || ["", ""];
  const main = me.display || me.user;
  const sub = me.display && me.login ? `<small class="muted">${esc(me.login)}</small>` : "";
  const winBtn = me.windows_auth && me.source !== "windows"
    ? `<button type="button" class="btn" id="win-login">Войти через Windows</button>` : "";
  $("#user").innerHTML = `<span class="label">Вы вошли как</span><b>${esc(main)}</b>${sub}${badge(label, color)}${winBtn}`;
  const btn = $("#win-login");
  if (btn) {
    btn.onclick = () => guarded(async () => {
      await loginWindows();
      await loadUser({ tryWindows: false });
      toast("Вход по учётной записи Windows выполнен", true);
    });
  }
  if (me.source === "unknown") {
    if (!me.allow_manual) {
      toast("Не удалось определить учётную запись Windows. Просмотр доступен, изменения — нет. " +
        "Обратитесь к администратору (см. README, раздел «Определение пользователя»).");
      return;
    }
    openForm("Как вас зовут?",
      `<p class="muted">Не удалось автоматически определить вашу учётную запись Windows.
       Укажите имя — оно будет записываться в аудит с пометкой «введено вручную».</p>
       <label><span>Имя или логин</span><input name="name" required maxlength="100"></label>`,
      async (d) => { await api("POST", "/api/me", { name: d.name }); await loadUser({ tryWindows: false }); }, "Продолжить");
  }
}

// ---------- Остаток часов (таблица balances, одна на все окна) ----------
function balNum(n, planned = 0) {
  return `<b class="${n < 0 ? "neg" : ""}">${fmtNum(n)}</b>`
    + (planned ? `<small class="planned" title="Ещё начислится по графику, когда наступят дни дежурств">+${fmtNum(planned)} впереди</small>` : "");
}
function balanceOf(id) {
  const e = state.employees.find((x) => x.id === id);
  return e ? { official: e.balance_official, unofficial: e.balance_unofficial } : { official: 0, unofficial: 0 };
}
/** Карточка «Остаток часов»: начислено минус списано за всё время, по каждому дежурному. */
function balanceCard() {
  const items = state.employees.map((e) => `<li><div class="who"><b>${esc(e.full_name)}</b><small>${esc(e.position)}</small></div>
    <div class="nums"><span><em>офиц.</em>${balNum(e.balance_official, e.planned_official)}</span><span><em>неофиц.</em>${balNum(e.balance_unofficial, e.planned_unofficial)}</span></div></li>`).join("");
  return card("Остаток часов", items
    ? `<ul class="kv">${items}</ul><p class="muted" style="padding:0 16px 12px">За всё время: начислено минус списано. «Впереди» — часы будущих дежурств, начислятся в день дежурства.</p>`
    : '<div class="empty">Нет дежурных.</div>', { flush: true });
}

// ---------- Календарь (общий для графика дежурств и отгулов) ----------
/** items — записи месяца; dateOf(x) — дата записи; chip(x) — HTML плашки; addLabel — подсказка кнопки «+». */
function calendar(items, dateOf, chip, addLabel) {
  const [y, mo] = state.month.split("-").map(Number);
  const days = new Date(y, mo, 0).getDate();
  const offset = (new Date(y, mo - 1, 1).getDay() + 6) % 7; // неделя с понедельника
  const byDay = {};
  items.forEach((x) => (byDay[dateOf(x)] = byDay[dateOf(x)] || []).push(x));
  let cells = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((n) => `<div class="dow">${n}</div>`).join("");
  cells += '<div class="day out"></div>'.repeat(offset);
  for (let d = 1; d <= days; d++) {
    const date = `${state.month}-${pad(d)}`;
    const dow = (offset + d - 1) % 7;
    const cls = ["day", dow >= 5 ? "weekend" : "", date === todayStr() ? "today" : ""].join(" ");
    cells += `<div class="${cls}"><div class="dnum"><span>${pad(d)}</span>
      <button class="add" data-add="${date}" title="${esc(addLabel)}" aria-label="${esc(addLabel)} на ${date}">+</button></div>
      ${(byDay[date] || []).map(chip).join("")}</div>`;
  }
  return `<div class="calendar">${cells}</div>`;
}

// ---------- График дежурств ----------
function dutyFields(x) {
  const v = x || { kind: "official", hours: DEFAULT_DUTY_HOURS, role: "duty", note: "" };
  return `${x ? "" : `<label><span>Дежурный</span><select name="employee_id">${employeeOptions()}</select></label>`}
    <label><span>Тип дежурства</span><select name="kind">${options(DUTY_KIND, v.kind)}</select></label>
    <label><span>Роль</span><select name="role">${options(ROLE, v.role)}</select></label>
    <label><span>Часы (по умолчанию ${DEFAULT_DUTY_HOURS})</span><input type="number" name="hours" min="0.25" max="24" step="0.25" value="${v.hours}" required></label>
    <label><span>Примечание (необязательно)</span><input name="note" maxlength="300" value="${esc(v.note)}" placeholder="например, ночная смена"></label>
    <p class="muted">Часы автоматически попадут в таблицу «${esc(HOURS_KIND.official)}» или «${esc(HOURS_KIND.unofficial)}» (по типу дежурства) при наступлении дня дежурства.</p>`;
}

async function renderSchedule(seq) {
  const duties = await api("GET", `/api/duties?month=${state.month}`);
  if (isStale(seq)) return;
  const chip = (x) => {
    const title = `${x.full_name} — ${x.position}\n${DUTY_KIND[x.kind]} дежурство · ${ROLE[x.role] || x.role} · ${fmtNum(x.hours)} ч`
      + (x.note ? `\n${x.note}` : "") + (x.accrued_at ? "\nЧасы начислены" : "\nЧасы будут начислены в день дежурства");
    return `<div class="chip ${x.kind}" title="${esc(title)}">
      <button class="edit" data-edit-duty="${x.id}">${esc(x.full_name)}
        <small>${x.accrued_at ? '<b class="ok">✓</b> ' : ""}${esc(ROLE_SHORT[x.role] || x.role)} · ${fmtNum(x.hours)} ч</small></button>
      <button data-del-duty="${x.id}" title="Снять с дежурства" aria-label="Снять с дежурства">×</button></div>`;
  };

  const stats = {};
  duties.forEach((d) => {
    const s = (stats[d.employee_id] = stats[d.employee_id] || { name: d.full_name, position: d.position, n: 0, official: 0, unofficial: 0 });
    s.n++; s[d.kind] += d.hours;
  });
  const rank = Object.values(stats).sort((a, b) => b.n - a.n || a.name.localeCompare(b.name, "ru"))
    .map((c) => `<li><div class="who"><b>${esc(c.name)}</b><small>${esc(c.position)}</small></div>
      <div class="nums"><span><em>дежурств</em><b>${c.n}</b></span><span><em>офиц. ч</em><b>${fmtNum(c.official)}</b></span>
      <span><em>неофиц. ч</em><b>${fmtNum(c.unofficial)}</b></span></div></li>`).join("");
  const legend = `<div class="legend">${badge("Официальное", "green")}${badge("Неофициальное", "amber")}
    <span>Деж. — дежурный, Пом. — помощник дежурного · ✓ — часы начислены · нажмите на плашку, чтобы изменить</span></div>`;

  view.innerHTML = `<div class="split">
    ${card(monthTitle(state.month), calendar(duties, (d) => d.duty_date, chip, "Назначить дежурного") + legend,
      { right: badge(`${duties.length} ${plural(duties.length, ["дежурство", "дежурства", "дежурств"])}`, duties.length ? "green" : "") })}
    <aside class="rail">${card("По графику за месяц", rank ? `<ul class="kv">${rank}</ul>` : '<div class="empty">В этом месяце дежурств нет.</div>', { flush: true })}
      ${balanceCard()}</aside>
  </div>`;

  const payload = (d) => ({ kind: d.kind, role: d.role, hours: Number(d.hours), note: d.note });
  view.querySelectorAll("[data-add]").forEach((btn) => (btn.onclick = () => {
    if (!state.employees.length) return toast("Сначала добавьте дежурных на вкладке «Дежурные»");
    const date = btn.dataset.add;
    openForm(`Дежурство ${date}`, dutyFields(),
      async (d) => { await api("POST", "/api/duties", { date, employee_id: Number(d.employee_id), ...payload(d) }); await render(); },
      "Назначить");
  }));
  view.querySelectorAll("[data-edit-duty]").forEach((btn) => (btn.onclick = () => {
    const x = duties.find((d) => d.id === Number(btn.dataset.editDuty));
    openForm(`Дежурство ${x.duty_date}: ${x.full_name}`, dutyFields(x),
      async (d) => { await api("PUT", `/api/duties/${x.id}`, payload(d)); await render(); });
  }));
  view.querySelectorAll("[data-del-duty]").forEach((btn) => (btn.onclick = () => {
    const x = duties.find((d) => d.id === Number(btn.dataset.delDuty));
    if (x.accrued_at && x.accrued_at !== "до автоначисления"
        && !confirm(`Снять ${x.full_name} с дежурства ${x.duty_date}?\nНачисленные ${fmtNum(x.hours)} ч будут удалены из таблицы «${HOURS_KIND[x.kind]}».`)) return;
    guarded(async () => { await api("DELETE", `/api/duties/${x.id}`); await render(); });
  }));
}

// ---------- График отгулов ----------
function dayoffFields(x) {
  const v = x || { kind: "official", hours: DEFAULT_DAYOFF_HOURS, note: "" };
  return `${x ? "" : `<label><span>Сотрудник</span><select name="employee_id">${employeeOptions()}</select></label>`}
    <label><span>Списать часы из</span><select name="kind">${options(HOURS_KIND, v.kind)}</select></label>
    <label><span>Часы (по умолчанию ${DEFAULT_DAYOFF_HOURS})</span><input type="number" name="hours" min="0.25" max="24" step="0.25" value="${v.hours}" required></label>
    <label><span>Примечание (необязательно)</span><input name="note" maxlength="300" value="${esc(v.note)}"></label>
    <p class="muted">Часы сразу спишутся из выбранной таблицы (запись со знаком «−» на дату отгула). При отмене отгула часы вернутся.</p>`;
}

async function renderDayoffs(seq) {
  const offs = await api("GET", `/api/dayoffs?month=${state.month}`);
  if (isStale(seq)) return;
  const chip = (x) => {
    const title = `${x.full_name} — ${x.position}\nОтгул: списано ${fmtNum(x.hours)} ч из «${HOURS_KIND[x.kind]}»` + (x.note ? `\n${x.note}` : "");
    return `<div class="chip off" title="${esc(title)}">
      <button class="edit" data-edit-off="${x.id}">${esc(x.full_name)}
        <small>−${fmtNum(x.hours)} ч ${x.kind === "official" ? "офиц." : "неофиц."}</small></button>
      <button data-del-off="${x.id}" title="Отменить отгул" aria-label="Отменить отгул">×</button></div>`;
  };
  const total = offs.reduce((s, x) => s + x.hours, 0);

  view.innerHTML = `<div class="split">
    ${card(monthTitle(state.month), calendar(offs, (x) => x.off_date, chip, "Поставить отгул")
      + '<div class="legend"><span>Нажмите на плашку, чтобы изменить отгул; × — отменить и вернуть часы</span></div>',
      { right: badge(`${offs.length} ${plural(offs.length, ["отгул", "отгула", "отгулов"])} · ${fmtNum(total)} ч`, offs.length ? "red" : "") })}
    <aside class="rail">${balanceCard()}</aside>
  </div>`;

  const payload = (d) => ({ kind: d.kind, hours: Number(d.hours), note: d.note });
  const warnIfShort = (id, d, already = 0) => {
    const left = balanceOf(id)[d.kind] + already - Number(d.hours);
    return left >= 0 || confirm(`После списания остаток в «${HOURS_KIND[d.kind]}» станет ${fmtNum(left)} ч (меньше нуля). Всё равно поставить отгул?`);
  };
  view.querySelectorAll("[data-add]").forEach((btn) => (btn.onclick = () => {
    if (!state.employees.length) return toast("Сначала добавьте дежурных на вкладке «Дежурные»");
    const date = btn.dataset.add;
    openForm(`Отгул ${date}`, dayoffFields(), async (d) => {
      if (!warnIfShort(Number(d.employee_id), d)) throw new Error("Отгул не поставлен");
      await api("POST", "/api/dayoffs", { date, employee_id: Number(d.employee_id), ...payload(d) });
      await render();
      toast(`Списано ${fmtNum(d.hours)} ч из «${HOURS_KIND[d.kind]}»`, true);
    }, "Поставить отгул");
  }));
  view.querySelectorAll("[data-edit-off]").forEach((btn) => (btn.onclick = () => {
    const x = offs.find((o) => o.id === Number(btn.dataset.editOff));
    openForm(`Отгул ${x.off_date}: ${x.full_name}`, dayoffFields(x), async (d) => {
      if (!warnIfShort(x.employee_id, d, d.kind === x.kind ? x.hours : 0)) throw new Error("Отгул не изменён");
      await api("PUT", `/api/dayoffs/${x.id}`, payload(d));
      await render();
    });
  }));
  view.querySelectorAll("[data-del-off]").forEach((btn) => (btn.onclick = () => {
    const x = offs.find((o) => o.id === Number(btn.dataset.delOff));
    if (!confirm(`Отменить отгул ${x.full_name} ${x.off_date}?\n${fmtNum(x.hours)} ч вернутся в «${HOURS_KIND[x.kind]}».`)) return;
    guarded(async () => { await api("DELETE", `/api/dayoffs/${x.id}`); await render(); toast("Отгул отменён, часы возвращены", true); });
  }));
}

// ---------- Часы ----------
function hoursFields(row) {
  const date = row ? row.work_date : (state.month === currentMonth() ? todayStr() : `${state.month}-01`);
  return `<label><span>Дата</span><input type="date" name="date" value="${date}" required></label>
    <label><span>Сотрудник</span><select name="employee_id">${employeeOptions(row && row.employee_id, row)}</select></label>
    <label><span>Часы</span><input type="number" name="hours" min="0.25" max="24" step="0.25" value="${row ? row.hours : ""}" required></label>
    <label><span>Комментарий</span><input name="comment" maxlength="500" value="${esc(row ? row.comment : "")}"></label>`;
}

/** Живые проверки формы: статус, текст. Сервер всё равно проверяет данные сам. */
/** Дежурство того же типа у сотрудника в этот день: его часы уже начисляются автоматически. */
function sameKindDuty(duties, kind, date, employeeId) {
  return duties.find((d) => d.duty_date === date && String(d.employee_id) === String(employeeId) && d.kind === kind);
}

function hoursChecks(form, duties, kind) {
  const v = Object.fromEntries(new FormData(form));
  const hrs = parseFloat(v.hours);
  const items = [];
  items.push(!v.hours ? ["idle", "Часы: от 0,25 до 24"]
    : hrs >= 0.25 && hrs <= 24 ? ["ok", "Часы в допустимом диапазоне"] : ["bad", "Часы вне диапазона 0,25–24"]);
  items.push(v.employee_id ? ["ok", "Сотрудник выбран"] : ["bad", "Выберите сотрудника"]);
  const inMonth = v.date && v.date.slice(0, 7) === state.month;
  items.push(!v.date ? ["bad", "Укажите дату"]
    : inMonth ? ["ok", "Дата в выбранном месяце"] : ["warn", "Дата вне выбранного месяца — запись попадёт в другой месяц"]);
  if (inMonth && v.employee_id) {
    const same = sameKindDuty(duties, kind, v.date, v.employee_id);
    const other = duties.find((d) => d.duty_date === v.date && String(d.employee_id) === String(v.employee_id) && d.kind !== kind);
    items.push(same ? ["warn", `В этот день у сотрудника ${DUTY_KIND[kind].toLowerCase()} дежурство: его ${fmtNum(same.hours)} ч начисляются автоматически, ручная запись их задвоит`]
      : other ? ["ok", `Дежурство в этот день ${DUTY_KIND[other.kind].toLowerCase()} — его часы идут в другую таблицу`]
      : ["ok", "Дежурства в этот день нет — переработка вне графика"]);
  }
  const icon = { ok: "✓", warn: "!", bad: "✕", idle: "•" };
  return items.map(([s, t]) => `<li class="${s}"><i>${icon[s]}</i><span>${esc(t)}</span></li>`).join("");
}

async function renderHours(kind, seq) {
  const [rows, summary, duties] = await Promise.all([
    api("GET", `/api/hours/${kind}?month=${state.month}`),
    api("GET", `/api/hours/summary?month=${state.month}`),
    api("GET", `/api/duties?month=${state.month}`),
  ]);
  if (isStale(seq)) return;
  const total = rows.reduce((s, r) => s + r.hours, 0);
  const SOURCE_BADGE = { duty: ["Дежурство", KIND_BADGE[kind]], dayoff: ["Отгул", "red"], manual: ["Вручную", ""] };
  const body = rows.map((r) => {
    const [label, color] = SOURCE_BADGE[r.source] || SOURCE_BADGE.manual;
    const actions = r.source === "manual"
      ? `<button class="btn sm warn" data-edit="${r.id}">Изменить</button><button class="btn sm danger" data-del="${r.id}">Удалить</button>`
      : `<span class="muted" title="Запись создана по графику — меняйте её в графике ${r.source === "duty" ? "дежурств" : "отгулов"}">авто</span>`;
    return `<tr><td class="mono nowrap">${esc(r.work_date)}</td>
    <td>${esc(r.full_name)}<span class="sub">${esc(r.position)}</span></td>
    <td>${badge(label, color)}</td>
    <td class="num ${r.hours < 0 ? "neg" : "pos"}">${fmtSigned(r.hours)}</td><td>${esc(r.comment)}</td><td class="mono muted">${esc(r.created_by)}</td>
    <td class="actions">${actions}</td></tr>`;
  }).join("");
  const sumRows = summary.map((s) => `<li><div class="who"><b>${esc(s.full_name)}</b><small>${esc(s.position)}</small></div>
    <div class="nums"><span><em>офиц.</em><b>${fmtNum(s.official)}</b></span><span><em>неофиц.</em><b>${fmtNum(s.unofficial)}</b></span>
    <span><em>всего</em><b>${fmtNum(s.total)}</b></span></div></li>`).join("");

  const table = rows.length
    ? `<div class="table-wrap"><table><thead><tr><th>Дата</th><th>Сотрудник</th><th>Основание</th><th class="num">Часы</th><th>Тип дежурства / комментарий</th><th>Внёс</th><th></th></tr></thead>
       <tbody>${body}</tbody><tfoot><tr><td colspan="3" title="Начислено минус списано">Итого за месяц</td><td class="num">${fmtNum(total)}</td><td colspan="3"></td></tr></tfoot></table></div>`
    : '<div class="empty">Записей за этот месяц нет.</div>';

  view.innerHTML = `<div class="split">
    ${card(`${TABS[kind][1]} · ${monthTitle(state.month)}`, table,
      { flush: true, right: badge(`${fmtNum(total)} ч`, KIND_BADGE[kind]) })}
    <aside class="rail">
      ${card("Новая запись", `<form id="addform" class="stack">${hoursFields()}
        <ul id="checks" class="checks" aria-live="polite"></ul>
        <button class="btn approve block" type="submit">Добавить</button></form>
        ${state.employees.length ? "" : '<p class="muted">Сначала добавьте дежурных на вкладке «Дежурные».</p>'}`)}
      ${card("Сводка за месяц", sumRows ? `<ul class="kv">${sumRows}</ul>` : '<div class="empty">Нет данных за месяц.</div>', { flush: true })}
      ${balanceCard()}
    </aside></div>`;

  const form = $("#addform");
  const refreshChecks = () => ($("#checks").innerHTML = hoursChecks(form, duties, kind));
  form.addEventListener("input", refreshChecks);
  form.addEventListener("change", refreshChecks);
  refreshChecks();
  form.onsubmit = (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(form));
    const same = sameKindDuty(duties, kind, d.date, d.employee_id);
    if (same && !confirm(`Часы за дежурство ${d.date} (${fmtNum(same.hours)} ч) уже начисляются в эту таблицу автоматически.\nДобавить ещё одну запись вручную?`)) return;
    guarded(async () => {
      await api("POST", `/api/hours/${kind}`, { date: d.date, employee_id: Number(d.employee_id), hours: Number(d.hours), comment: d.comment });
      await render();
      toast("Запись добавлена", true);
    });
  };
  view.querySelectorAll("[data-edit]").forEach((btn) => (btn.onclick = () => {
    const row = rows.find((r) => r.id === Number(btn.dataset.edit));
    openForm("Изменить запись", hoursFields(row),
      async (d) => {
        await api("PUT", `/api/hours/${kind}/${row.id}`, { date: d.date, employee_id: Number(d.employee_id), hours: Number(d.hours), comment: d.comment });
        await render();
      });
  }));
  view.querySelectorAll("[data-del]").forEach((btn) => (btn.onclick = () => {
    if (!confirm("Удалить запись? Действие будет записано в аудит.")) return;
    guarded(async () => { await api("DELETE", `/api/hours/${kind}/${btn.dataset.del}`); await render(); });
  }));
}

// ---------- Дежурные ----------
function employeeFields(e) {
  return `<label><span>ФИО</span><input name="full_name" required maxlength="200" value="${esc(e ? e.full_name : "")}"></label>
    <label><span>Должность</span><input name="position" required maxlength="200" value="${esc(e ? e.position : "")}"></label>`;
}

async function renderEmployees() {
  const rows = state.employees.map((e) => `<tr><td>${esc(e.full_name)}</td><td>${esc(e.position)}</td>
    <td class="num">${balNum(e.balance_official, e.planned_official)}</td>
    <td class="num">${balNum(e.balance_unofficial, e.planned_unofficial)}</td>
    <td class="actions"><button class="btn sm warn" data-edit="${e.id}">Изменить</button><button class="btn sm danger" data-del="${e.id}">Удалить</button></td></tr>`).join("");
  const table = rows
    ? `<div class="table-wrap"><table><thead><tr><th>ФИО</th><th>Должность</th><th class="num" title="Остаток за всё время: начислено минус списано">Офиц. ч</th><th class="num" title="Остаток за всё время: начислено минус списано">Неофиц. ч</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<div class="empty">Список пуст. Добавьте первого дежурного.</div>';
  view.innerHTML = `<div class="split">
    ${card("Список дежурных", table, { flush: true, right: badge(String(state.employees.length), "", true) })}
    <aside class="rail">${card("Добавить дежурного", `<form id="addform" class="stack">${employeeFields()}
      <button class="btn approve block" type="submit">Добавить</button></form>`)}</aside></div>`;

  const form = $("#addform");
  form.onsubmit = (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(form));
    guarded(async () => { await api("POST", "/api/employees", d); await render(); toast("Дежурный добавлен", true); });
  };
  view.querySelectorAll("[data-edit]").forEach((btn) => (btn.onclick = () => {
    const emp = state.employees.find((x) => x.id === Number(btn.dataset.edit));
    openForm("Изменить дежурного", employeeFields(emp), async (d) => { await api("PUT", `/api/employees/${emp.id}`, d); await render(); });
  }));
  view.querySelectorAll("[data-del]").forEach((btn) => (btn.onclick = () => {
    const emp = state.employees.find((x) => x.id === Number(btn.dataset.del));
    if (!confirm(`Удалить «${emp.full_name}» из списка дежурных?\nБудущие дежурства будут сняты, история часов сохранится.`)) return;
    guarded(async () => {
      const r = await api("DELETE", `/api/employees/${emp.id}`);
      await render();
      toast(r.removed_future_duties || r.removed_future_dayoffs
        ? `Удалено. Снято будущих дежурств: ${r.removed_future_duties}, отгулов: ${r.removed_future_dayoffs}` : "Дежурный удалён", true);
    });
  }));
}

// ---------- Аудит ----------
const auditFilters = { entity: "", actor: "", q: "" };

function auditDiff(item) {
  const { old, new: nw } = item;
  if (old && nw) {
    return Object.keys(nw).map((k) => `<div>${esc(k)}: <del>${esc(old[k])}</del> → <ins>${esc(nw[k])}</ins></div>`).join("");
  }
  const data = nw || old || {};
  return Object.entries(data).map(([k, v]) => `<div>${esc(k)}: ${esc(v)}</div>`).join("");
}

function auditRow(i) {
  const [label, color] = ACTION[i.action] || [i.action, ""];
  return `<tr><td class="mono">${esc(i.ts)}</td><td class="mono">${esc(i.actor)}<span class="sub">${esc(i.ip)}</span></td>
    <td>${badge(label, color)}</td><td>${esc(i.entity_label)}</td>
    <td>${esc(i.summary)}<div class="audit-diff">${auditDiff(i)}</div></td></tr>`;
}

async function loadAudit(append) {
  if (!append) state.auditOffset = 0;
  const params = new URLSearchParams({ limit: 50, offset: state.auditOffset });
  Object.entries(auditFilters).forEach(([k, v]) => v && params.set(k, v));
  const data = await api("GET", `/api/audit?${params}`);
  const tbody = $("#audit-body");
  if (!tbody) return; // пока шёл запрос, открыли другую вкладку
  const html = data.items.map(auditRow).join("");
  if (append) tbody.insertAdjacentHTML("beforeend", html);
  else tbody.innerHTML = html || '<tr><td colspan="5" class="muted">Записей нет.</td></tr>';
  state.auditOffset += data.items.length;
  $("#audit-count").textContent = `Показано ${state.auditOffset} из ${data.total}`;
  $("#audit-more").hidden = state.auditOffset >= data.total;
}

async function renderAudit() {
  view.innerHTML = `<div class="split single">
    ${card("Фильтры", `<form id="auditform" class="filters">
      <label><span>Раздел</span><select name="entity"><option value="">Все</option>
        <option value="employee">Список дежурных</option><option value="duty">График дежурств</option><option value="dayoff">График отгулов</option>
        <option value="hours_official">Официальные часы</option><option value="hours_unofficial">Неофициальные часы</option></select></label>
      <label><span>Кто изменил</span><input name="actor" value="${esc(auditFilters.actor)}"></label>
      <label class="grow"><span>Поиск по описанию</span><input name="q" value="${esc(auditFilters.q)}"></label>
      <button class="btn approve" type="submit">Применить</button></form>`)}
    <div class="card"><div class="table-wrap"><table>
      <thead><tr><th>Когда</th><th>Кто</th><th>Действие</th><th>Где</th><th>Что и на что изменено</th></tr></thead>
      <tbody id="audit-body"></tbody></table></div>
      <div class="pager"><span id="audit-count"></span><button id="audit-more" class="btn sm" hidden>Показать ещё</button></div></div>
  </div>`;
  const form = $("#auditform");
  form.elements.entity.value = auditFilters.entity;
  form.onsubmit = (e) => {
    e.preventDefault();
    Object.assign(auditFilters, Object.fromEntries(new FormData(form)));
    guarded(() => loadAudit(false));
  };
  $("#audit-more").onclick = () => guarded(() => loadAudit(true));
  await loadAudit(false);
}

// ---------- Каркас ----------
async function refreshHistory() {
  const months = await api("GET", "/api/months");
  if (!months.includes(state.month)) months.unshift(state.month);
  $("#history").innerHTML = `<option value="">Месяцы с данными…</option>` +
    months.sort().reverse().map((m) => `<option value="${m}">${esc(monthTitle(m))}</option>`).join("");
}

/** Номер текущей отрисовки. Пока ждали ответа сервера, пользователь мог открыть другую вкладку или месяц:
 *  тогда устаревшая отрисовка ничего не выводит, иначе данные графика дежурств попадали бы под заголовок
 *  «График отгулов» или таблицы часов (а «+» в таком календаре назначал бы дежурство, а не отгул). */
let renderSeq = 0;
function isStale(seq) { return seq !== renderSeq; }

async function render() {
  const seq = ++renderSeq;
  const usesMonth = ["schedule", "dayoffs", "official", "unofficial"].includes(state.tab);
  $("#monthbar").hidden = !usesMonth;
  $("#month").value = state.month;
  $("#eyebrow").textContent = TABS[state.tab][0];
  $("#title").textContent = TABS[state.tab][1];
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
  await guarded(async () => {
    const employees = await api("GET", "/api/employees");
    if (isStale(seq)) return;
    state.employees = employees;
    if (usesMonth) await refreshHistory();
    if (isStale(seq)) return;
    if (state.tab === "schedule") await renderSchedule(seq);
    else if (state.tab === "dayoffs") await renderDayoffs(seq);
    else if (state.tab === "employees") await renderEmployees();
    else if (state.tab === "audit") await renderAudit();
    else await renderHours(state.tab, seq);
  });
}

function setMonth(m) { if (/^\d{4}-\d{2}$/.test(m)) { state.month = m; render(); } }

$("#tabs").onclick = (e) => {
  const btn = e.target.closest("[data-tab]");
  if (btn) { state.tab = btn.dataset.tab; render(); }
};
$("#prev").onclick = () => setMonth(shiftMonth(state.month, -1));
$("#next").onclick = () => setMonth(shiftMonth(state.month, 1));
$("#today").onclick = () => setMonth(currentMonth());
$("#month").onchange = (e) => setMonth(e.target.value);
$("#history").onchange = (e) => setMonth(e.target.value);

loadUser().catch((err) => toast(err.message)).then(render);
