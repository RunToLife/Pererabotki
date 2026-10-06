"use strict";

const MONTH_NAMES = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
  "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
const SOURCE_LABEL = { os: "учётная запись ОС", proxy: "вход через прокси (SSO)",
  manual: "введено вручную", unknown: "не определено" };
const ACTION_LABEL = { create: "Создание", update: "Изменение", delete: "Удаление" };
const HOURS_TITLE = { official: "Официальные часы", unofficial: "Неофициальные часы" };

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
function monthTitle(m) { const [y, mo] = m.split("-").map(Number); return `${MONTH_NAMES[mo - 1]} ${y}`; }
function shiftMonth(m, delta) {
  const [y, mo] = m.split("-").map(Number);
  const d = new Date(y, mo - 1 + delta, 1);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
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
    <div class="actions"><button type="button" value="cancel" id="dlg-cancel">Отмена</button>
    <button type="submit" class="primary">${esc(submitLabel)}</button></div></form>`;
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

function employeeOptions(selectedId) {
  return state.employees.map((e) =>
    `<option value="${e.id}" ${e.id === selectedId ? "selected" : ""}>${esc(e.full_name)} — ${esc(e.position)}</option>`).join("");
}

// ---------- Пользователь ----------
async function loadUser() {
  const me = await api("GET", "/api/me");
  $("#user").innerHTML = `Вы: <b>${esc(me.user)}</b> · ${SOURCE_LABEL[me.source] || ""}`;
  if (me.source === "unknown") {
    openForm("Как вас зовут?",
      `<p class="muted">Не удалось автоматически определить вашу учётную запись ОС.
       Укажите имя — оно будет записываться в аудит.</p>
       <label>Имя или логин<input name="name" required maxlength="100"></label>`,
      async (d) => { await api("POST", "/api/me", { name: d.name }); await loadUser(); }, "Продолжить");
  }
}

// ---------- График дежурств ----------
async function renderSchedule() {
  const duties = await api("GET", `/api/duties?month=${state.month}`);
  const [y, mo] = state.month.split("-").map(Number);
  const days = new Date(y, mo, 0).getDate();
  const offset = (new Date(y, mo - 1, 1).getDay() + 6) % 7; // неделя с понедельника
  const byDay = {};
  duties.forEach((d) => (byDay[d.duty_date] = byDay[d.duty_date] || []).push(d));

  let cells = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((n) => `<div class="dow">${n}</div>`).join("");
  cells += '<div class="day out"></div>'.repeat(offset);
  for (let d = 1; d <= days; d++) {
    const date = `${state.month}-${pad(d)}`;
    const dow = (offset + d - 1) % 7;
    const cls = ["day", dow >= 5 ? "weekend" : "", date === todayStr() ? "today" : ""].join(" ");
    const chips = (byDay[date] || []).map((x) =>
      `<div class="chip" title="${esc(x.full_name)} — ${esc(x.position)}${x.note ? "\n" + esc(x.note) : ""}">
         <span>${esc(x.full_name)}${x.note ? " · " + esc(x.note) : ""}</span>
         <button data-del-duty="${x.id}" title="Снять с дежурства">×</button></div>`).join("");
    cells += `<div class="${cls}"><div class="dnum"><span>${d}</span>
      <button class="add" data-add-duty="${date}" title="Назначить дежурного">+</button></div>${chips}</div>`;
  }

  const counts = {};
  duties.forEach((d) => {
    counts[d.employee_id] = counts[d.employee_id] || { name: d.full_name, position: d.position, n: 0 };
    counts[d.employee_id].n++;
  });
  const countRows = Object.values(counts).sort((a, b) => a.name.localeCompare(b.name, "ru"))
    .map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(c.position)}</td><td class="num">${c.n}</td></tr>`).join("");

  view.innerHTML = `<div class="card"><h2>График дежурств — ${esc(monthTitle(state.month))}</h2>
    <div class="calendar">${cells}</div></div>
    <div class="card"><h2>Количество дежурств за месяц</h2>
    ${countRows ? `<div class="table-wrap"><table><thead><tr><th>ФИО</th><th>Должность</th><th class="num">Дежурств</th></tr></thead>
      <tbody>${countRows}</tbody></table></div>` : '<p class="muted">В этом месяце дежурств нет.</p>'}</div>`;

  view.querySelectorAll("[data-add-duty]").forEach((btn) => (btn.onclick = () => {
    if (!state.employees.length) return toast("Сначала добавьте дежурных на вкладке «Дежурные»");
    const date = btn.dataset.addDuty;
    openForm(`Дежурство ${date}`,
      `<label>Дежурный<select name="employee_id">${employeeOptions()}</select></label>
       <label>Примечание (необязательно)<input name="note" maxlength="300" placeholder="например, ночная смена"></label>`,
      async (d) => { await api("POST", "/api/duties", { date, employee_id: Number(d.employee_id), note: d.note }); await render(); },
      "Назначить");
  }));
  view.querySelectorAll("[data-del-duty]").forEach((btn) => (btn.onclick = () =>
    guarded(async () => { await api("DELETE", `/api/duties/${btn.dataset.delDuty}`); await render(); })));
}

// ---------- Часы ----------
function hoursForm(row) {
  const date = row ? row.work_date : (state.month === currentMonth() ? todayStr() : `${state.month}-01`);
  return `<label>Дата<input type="date" name="date" value="${date}" required></label>
    <label>Сотрудник<select name="employee_id">${employeeOptions(row && row.employee_id)}</select></label>
    <label>Часы<input type="number" name="hours" min="0.25" max="24" step="0.25" value="${row ? row.hours : ""}" required></label>
    <label>Комментарий<input name="comment" maxlength="500" value="${esc(row ? row.comment : "")}"></label>`;
}

async function renderHours(kind) {
  const [rows, summary] = await Promise.all([
    api("GET", `/api/hours/${kind}?month=${state.month}`),
    api("GET", `/api/hours/summary?month=${state.month}`),
  ]);
  const total = rows.reduce((s, r) => s + r.hours, 0);
  const body = rows.map((r) => `<tr><td>${esc(r.work_date)}</td><td>${esc(r.full_name)}<br><span class="muted">${esc(r.position)}</span></td>
    <td class="num">${fmtNum(r.hours)}</td><td>${esc(r.comment)}</td><td class="muted">${esc(r.created_by)}</td>
    <td><button class="link" data-edit="${r.id}">Изменить</button><button class="link danger" data-del="${r.id}">Удалить</button></td></tr>`).join("");
  const sumRows = summary.map((s) => `<tr><td>${esc(s.full_name)}</td><td>${esc(s.position)}</td>
    <td class="num">${fmtNum(s.official)}</td><td class="num">${fmtNum(s.unofficial)}</td><td class="num"><b>${fmtNum(s.total)}</b></td></tr>`).join("");

  view.innerHTML = `<div class="card"><h2>${HOURS_TITLE[kind]} — ${esc(monthTitle(state.month))}</h2>
    <form id="addform" class="formrow">${hoursForm()}<button class="primary" type="submit">Добавить</button></form></div>
    <div class="card"><div class="table-wrap"><table>
      <thead><tr><th>Дата</th><th>Сотрудник</th><th class="num">Часы</th><th>Комментарий</th><th>Внёс</th><th></th></tr></thead>
      <tbody>${body || '<tr><td colspan="6" class="muted">Записей за этот месяц нет.</td></tr>'}</tbody>
      <tfoot><tr><td colspan="2">Итого за месяц</td><td class="num">${fmtNum(total)}</td><td colspan="3"></td></tr></tfoot></table></div></div>
    <div class="card"><h2>Сводка по сотрудникам</h2>
    ${sumRows ? `<div class="table-wrap"><table><thead><tr><th>ФИО</th><th>Должность</th><th class="num">Официальные</th>
      <th class="num">Неофициальные</th><th class="num">Всего</th></tr></thead><tbody>${sumRows}</tbody></table></div>`
      : '<p class="muted">Нет данных за месяц.</p>'}</div>`;

  const form = $("#addform");
  if (!state.employees.length) form.insertAdjacentHTML("afterend", '<p class="muted">Сначала добавьте дежурных на вкладке «Дежурные».</p>');
  form.onsubmit = (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(form));
    guarded(async () => {
      await api("POST", `/api/hours/${kind}`, { date: d.date, employee_id: Number(d.employee_id), hours: Number(d.hours), comment: d.comment });
      await render();
      toast("Запись добавлена", true);
    });
  };
  view.querySelectorAll("[data-edit]").forEach((btn) => (btn.onclick = () => {
    const row = rows.find((r) => r.id === Number(btn.dataset.edit));
    openForm("Изменить запись", hoursForm(row),
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
function employeeForm(e) {
  return `<label>ФИО<input name="full_name" required maxlength="200" value="${esc(e ? e.full_name : "")}"></label>
    <label>Должность<input name="position" required maxlength="200" value="${esc(e ? e.position : "")}"></label>`;
}

async function renderEmployees() {
  const rows = state.employees.map((e) => `<tr><td>${esc(e.full_name)}</td><td>${esc(e.position)}</td>
    <td><button class="link" data-edit="${e.id}">Изменить</button><button class="link danger" data-del="${e.id}">Удалить</button></td></tr>`).join("");
  view.innerHTML = `<div class="card"><h2>Добавить дежурного</h2>
    <form id="addform" class="formrow"><label class="grow">ФИО<input name="full_name" required maxlength="200"></label>
    <label class="grow">Должность<input name="position" required maxlength="200"></label>
    <button class="primary" type="submit">Добавить</button></form></div>
    <div class="card"><h2>Список дежурных (${state.employees.length})</h2><div class="table-wrap"><table>
    <thead><tr><th>ФИО</th><th>Должность</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="3" class="muted">Список пуст. Добавьте первого дежурного.</td></tr>'}</tbody></table></div></div>`;

  const form = $("#addform");
  form.onsubmit = (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(form));
    guarded(async () => { await api("POST", "/api/employees", d); await render(); toast("Дежурный добавлен", true); });
  };
  view.querySelectorAll("[data-edit]").forEach((btn) => (btn.onclick = () => {
    const emp = state.employees.find((x) => x.id === Number(btn.dataset.edit));
    openForm("Изменить дежурного", employeeForm(emp), async (d) => { await api("PUT", `/api/employees/${emp.id}`, d); await render(); });
  }));
  view.querySelectorAll("[data-del]").forEach((btn) => (btn.onclick = () => {
    const emp = state.employees.find((x) => x.id === Number(btn.dataset.del));
    if (!confirm(`Удалить «${emp.full_name}» из списка дежурных?\nБудущие дежурства будут сняты, история часов сохранится.`)) return;
    guarded(async () => {
      const r = await api("DELETE", `/api/employees/${emp.id}`);
      await render();
      toast(r.removed_future_duties ? `Удалено. Снято будущих дежурств: ${r.removed_future_duties}` : "Дежурный удалён", true);
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
  return `<tr><td>${esc(i.ts)}</td><td>${esc(i.actor)}<br><span class="muted">${esc(i.ip)}</span></td>
    <td><span class="tag ${esc(i.action)}">${esc(ACTION_LABEL[i.action] || i.action)}</span></td>
    <td>${esc(i.entity_label)}</td><td>${esc(i.summary)}<div class="audit-diff muted">${auditDiff(i)}</div></td></tr>`;
}

async function loadAudit(append) {
  if (!append) state.auditOffset = 0;
  const params = new URLSearchParams({ limit: 50, offset: state.auditOffset });
  Object.entries(auditFilters).forEach(([k, v]) => v && params.set(k, v));
  const data = await api("GET", `/api/audit?${params}`);
  const tbody = $("#audit-body");
  const html = data.items.map(auditRow).join("");
  if (append) tbody.insertAdjacentHTML("beforeend", html);
  else tbody.innerHTML = html || '<tr><td colspan="5" class="muted">Записей нет.</td></tr>';
  state.auditOffset += data.items.length;
  $("#audit-count").textContent = `Показано ${state.auditOffset} из ${data.total}`;
  $("#audit-more").hidden = state.auditOffset >= data.total;
}

async function renderAudit() {
  view.innerHTML = `<div class="card"><h2>Журнал аудита</h2>
    <form id="auditform" class="formrow">
      <label>Раздел<select name="entity"><option value="">Все</option>
        <option value="employee">Список дежурных</option><option value="duty">График дежурств</option>
        <option value="hours_official">Официальные часы</option><option value="hours_unofficial">Неофициальные часы</option></select></label>
      <label>Кто изменил<input name="actor" value="${esc(auditFilters.actor)}"></label>
      <label class="grow">Поиск по описанию<input name="q" value="${esc(auditFilters.q)}"></label>
      <button class="primary" type="submit">Применить</button></form></div>
    <div class="card"><div class="table-wrap"><table>
      <thead><tr><th>Когда</th><th>Кто</th><th>Действие</th><th>Где</th><th>Что и на что изменено</th></tr></thead>
      <tbody id="audit-body"></tbody></table></div>
      <p><span id="audit-count" class="muted"></span> <button id="audit-more" hidden>Показать ещё</button></p></div>`;
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

async function render() {
  const usesMonth = ["schedule", "official", "unofficial"].includes(state.tab);
  $("#monthbar").hidden = !usesMonth;
  $("#month").value = state.month;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
  await guarded(async () => {
    state.employees = await api("GET", "/api/employees");
    if (usesMonth) await refreshHistory();
    if (state.tab === "schedule") await renderSchedule();
    else if (state.tab === "employees") await renderEmployees();
    else if (state.tab === "audit") await renderAudit();
    else await renderHours(state.tab);
  });
}

function setMonth(m) { if (/^\d{4}-\d{2}$/.test(m)) { state.month = m; render(); } }

$("#tabs").onclick = (e) => { const t = e.target.dataset.tab; if (t) { state.tab = t; render(); } };
$("#prev").onclick = () => setMonth(shiftMonth(state.month, -1));
$("#next").onclick = () => setMonth(shiftMonth(state.month, 1));
$("#today").onclick = () => setMonth(currentMonth());
$("#month").onchange = (e) => setMonth(e.target.value);
$("#history").onchange = (e) => setMonth(e.target.value);

loadUser().catch((err) => toast(err.message)).then(render);
