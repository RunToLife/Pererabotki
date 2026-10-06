"use strict";

const MONTH_NAMES = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
  "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
const SOURCE = {
  os: ["учётная запись ОС", "green"], proxy: ["SSO через прокси", "green"],
  manual: ["введено вручную", "amber"], unknown: ["не определено", "red"],
};
const ACTION = { create: ["Создание", "green"], update: ["Изменение", "amber"], delete: ["Удаление", "red"] };
const TABS = {
  schedule: ["Раздел 01", "График дежурств"], official: ["Раздел 02", "Официальные часы"],
  unofficial: ["Раздел 03", "Неофициальные часы"], employees: ["Раздел 04", "Дежурные"],
  audit: ["Раздел 05", "Журнал аудита"],
};
const KIND_BADGE = { official: "green", unofficial: "amber" };

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
async function loadUser() {
  const me = await api("GET", "/api/me");
  const [label, color] = SOURCE[me.source] || ["", ""];
  $("#user").innerHTML = `<span class="label">Вы вошли как</span><b>${esc(me.user)}</b>${badge(label, color)}`;
  if (me.source === "unknown") {
    openForm("Как вас зовут?",
      `<p class="muted">Не удалось автоматически определить вашу учётную запись ОС.
       Укажите имя — оно будет записываться в аудит.</p>
       <label><span>Имя или логин</span><input name="name" required maxlength="100"></label>`,
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
         <button data-del-duty="${x.id}" title="Снять с дежурства" aria-label="Снять с дежурства">×</button></div>`).join("");
    cells += `<div class="${cls}"><div class="dnum"><span>${pad(d)}</span>
      <button class="add" data-add-duty="${date}" title="Назначить дежурного" aria-label="Назначить дежурного на ${date}">+</button></div>${chips}</div>`;
  }

  const counts = {};
  duties.forEach((d) => {
    counts[d.employee_id] = counts[d.employee_id] || { name: d.full_name, position: d.position, n: 0 };
    counts[d.employee_id].n++;
  });
  const rank = Object.values(counts).sort((a, b) => b.n - a.n || a.name.localeCompare(b.name, "ru"))
    .map((c) => `<li><div class="who"><b>${esc(c.name)}</b><small>${esc(c.position)}</small></div>
      <div class="nums"><span><em>дежурств</em><b>${c.n}</b></span></div></li>`).join("");

  view.innerHTML = `<div class="split">
    ${card(monthTitle(state.month), `<div class="calendar">${cells}</div>`,
      { right: badge(`${duties.length} ${plural(duties.length, ["дежурство", "дежурства", "дежурств"])}`, duties.length ? "green" : "") })}
    <aside class="rail">${card("По сотрудникам", rank ? `<ul class="kv">${rank}</ul>` : '<div class="empty">В этом месяце дежурств нет.</div>', { flush: true })}</aside>
  </div>`;

  view.querySelectorAll("[data-add-duty]").forEach((btn) => (btn.onclick = () => {
    if (!state.employees.length) return toast("Сначала добавьте дежурных на вкладке «Дежурные»");
    const date = btn.dataset.addDuty;
    openForm(`Дежурство ${date}`,
      `<label><span>Дежурный</span><select name="employee_id">${employeeOptions()}</select></label>
       <label><span>Примечание (необязательно)</span><input name="note" maxlength="300" placeholder="например, ночная смена"></label>`,
      async (d) => { await api("POST", "/api/duties", { date, employee_id: Number(d.employee_id), note: d.note }); await render(); },
      "Назначить");
  }));
  view.querySelectorAll("[data-del-duty]").forEach((btn) => (btn.onclick = () =>
    guarded(async () => { await api("DELETE", `/api/duties/${btn.dataset.delDuty}`); await render(); })));
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
function hoursChecks(form, duties) {
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
    const onDuty = duties.some((d) => d.duty_date === v.date && String(d.employee_id) === String(v.employee_id));
    items.push(onDuty ? ["ok", "По графику сотрудник дежурит в этот день"] : ["warn", "По графику сотрудник в этот день не дежурит"]);
  }
  const icon = { ok: "✓", warn: "!", bad: "✕", idle: "•" };
  return items.map(([s, t]) => `<li class="${s}"><i>${icon[s]}</i><span>${esc(t)}</span></li>`).join("");
}

async function renderHours(kind) {
  const [rows, summary, duties] = await Promise.all([
    api("GET", `/api/hours/${kind}?month=${state.month}`),
    api("GET", `/api/hours/summary?month=${state.month}`),
    api("GET", `/api/duties?month=${state.month}`),
  ]);
  const total = rows.reduce((s, r) => s + r.hours, 0);
  const body = rows.map((r) => `<tr><td class="mono">${esc(r.work_date)}</td>
    <td>${esc(r.full_name)}<span class="sub">${esc(r.position)}</span></td>
    <td class="num">${fmtNum(r.hours)}</td><td>${esc(r.comment)}</td><td class="mono muted">${esc(r.created_by)}</td>
    <td class="actions"><button class="btn sm warn" data-edit="${r.id}">Изменить</button><button class="btn sm danger" data-del="${r.id}">Удалить</button></td></tr>`).join("");
  const sumRows = summary.map((s) => `<li><div class="who"><b>${esc(s.full_name)}</b><small>${esc(s.position)}</small></div>
    <div class="nums"><span><em>офиц.</em><b>${fmtNum(s.official)}</b></span><span><em>неофиц.</em><b>${fmtNum(s.unofficial)}</b></span>
    <span><em>всего</em><b>${fmtNum(s.total)}</b></span></div></li>`).join("");

  const table = rows.length
    ? `<div class="table-wrap"><table><thead><tr><th>Дата</th><th>Сотрудник</th><th class="num">Часы</th><th>Комментарий</th><th>Внёс</th><th></th></tr></thead>
       <tbody>${body}</tbody><tfoot><tr><td colspan="2">Итого за месяц</td><td class="num">${fmtNum(total)}</td><td colspan="3"></td></tr></tfoot></table></div>`
    : '<div class="empty">Записей за этот месяц нет.</div>';

  view.innerHTML = `<div class="split">
    ${card(`${TABS[kind][1]} · ${monthTitle(state.month)}`, table,
      { flush: true, right: badge(`${fmtNum(total)} ч`, KIND_BADGE[kind]) })}
    <aside class="rail">
      ${card("Новая запись", `<form id="addform" class="stack">${hoursFields()}
        <ul id="checks" class="checks" aria-live="polite"></ul>
        <button class="btn approve block" type="submit">Добавить</button></form>
        ${state.employees.length ? "" : '<p class="muted">Сначала добавьте дежурных на вкладке «Дежурные».</p>'}`)}
      ${card("Сводка по сотрудникам", sumRows ? `<ul class="kv">${sumRows}</ul>` : '<div class="empty">Нет данных за месяц.</div>', { flush: true })}
    </aside></div>`;

  const form = $("#addform");
  const refreshChecks = () => ($("#checks").innerHTML = hoursChecks(form, duties));
  form.addEventListener("input", refreshChecks);
  form.addEventListener("change", refreshChecks);
  refreshChecks();
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
    <td class="actions"><button class="btn sm warn" data-edit="${e.id}">Изменить</button><button class="btn sm danger" data-del="${e.id}">Удалить</button></td></tr>`).join("");
  const table = rows
    ? `<div class="table-wrap"><table><thead><tr><th>ФИО</th><th>Должность</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
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
        <option value="employee">Список дежурных</option><option value="duty">График дежурств</option>
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

async function render() {
  const usesMonth = ["schedule", "official", "unofficial"].includes(state.tab);
  $("#monthbar").hidden = !usesMonth;
  $("#month").value = state.month;
  $("#eyebrow").textContent = TABS[state.tab][0];
  $("#title").textContent = TABS[state.tab][1];
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
