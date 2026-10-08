"""Переработка — веб-сервис учета часов переработки.

Flask + SQLite. Запуск: python app/server.py
Настройки через переменные окружения:
  PERER_HOST         адрес прослушивания (по умолчанию 127.0.0.1)
  PERER_PORT         порт (по умолчанию 8080)
  PERER_DATA_DIR     папка с базой и логом (по умолчанию ./data рядом с проектом)
  PERER_TRUST_PROXY  1 — доверять заголовку X-Remote-User от обратного прокси (SSO)
  PERER_ACCRUE_INTERVAL  как часто (в секундах) проверять наступившие дежурства (по умолчанию 300)
"""
import getpass
import json
import logging
import os
import re
import sqlite3
import sys
import threading
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, Response, g, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name   TEXT NOT NULL,
    position    TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS duties (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    duty_date   TEXT NOT NULL,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    note        TEXT NOT NULL DEFAULT '',
    UNIQUE (duty_date, employee_id)
);
CREATE INDEX IF NOT EXISTS idx_duties_date ON duties(duty_date);
CREATE TABLE IF NOT EXISTS hours (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('official', 'unofficial')),
    work_date   TEXT NOT NULL,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    hours       REAL NOT NULL,
    comment     TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hours_date ON hours(work_date);
CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    ip        TEXT NOT NULL,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id INTEGER,
    summary   TEXT NOT NULL,
    old_json  TEXT,
    new_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE TABLE IF NOT EXISTS dayoffs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    off_date    TEXT NOT NULL,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    kind        TEXT NOT NULL CHECK (kind IN ('official', 'unofficial')),
    hours       REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (off_date, employee_id)
);
CREATE INDEX IF NOT EXISTS idx_dayoffs_date ON dayoffs(off_date);
"""

# Колонки, добавленные после первой версии: для существующих баз они добавляются через ALTER TABLE.
MIGRATIONS = {
    "duties": [
        ("kind", "TEXT NOT NULL DEFAULT 'official'"),
        ("hours", "REAL NOT NULL DEFAULT 24"),
        ("role", "TEXT NOT NULL DEFAULT 'duty'"),
        ("accrued_at", "TEXT"),
    ],
    "hours": [
        ("source", "TEXT NOT NULL DEFAULT 'manual'"),
        ("duty_id", "INTEGER"),
        ("dayoff_id", "INTEGER"),
    ],
}
POST_MIGRATION_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_hours_duty ON hours(duty_id) WHERE duty_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_hours_dayoff ON hours(dayoff_id) WHERE dayoff_id IS NOT NULL;
"""

KIND_LABEL = {"official": "Официальные часы", "unofficial": "Неофициальные часы"}
DUTY_KIND_LABEL = {"official": "Официальное", "unofficial": "Неофициальное"}
ROLE_LABEL = {"duty": "Дежурный", "assistant": "Помощник дежурного", "shift": "Смена", "other": "Иное"}
DEFAULT_DUTY_HOURS = 24
DEFAULT_DAYOFF_HOURS = 8
SYSTEM_ACTOR = "система"
ENTITY_LABEL = {
    "employee": "Список дежурных",
    "duty": "График дежурств",
    "dayoff": "График отгулов",
    "hours_official": "Официальные часы",
    "hours_unofficial": "Неофициальные часы",
}
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def now_str():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def parse_date(value, field="Дата"):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError):
        raise ApiError(f"{field}: ожидается дата в формате ГГГГ-ММ-ДД")


def parse_month(value):
    if not value or not MONTH_RE.match(value):
        raise ApiError("Параметр month: ожидается ГГГГ-ММ")
    return value


def clean_text(value, field, required=False, max_len=300):
    text = ("" if value is None else str(value)).strip()
    if required and not text:
        raise ApiError(f"Поле «{field}» обязательно")
    if len(text) > max_len:
        raise ApiError(f"Поле «{field}» длиннее {max_len} символов")
    return text


def fmt_hours(value):
    return f"{value:g}"


def parse_hours(value, field="Часы"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ApiError(f"{field}: ожидается число")
    if not 0 < number <= 24:
        raise ApiError(f"{field}: допустимо значение больше 0 и не более 24")
    return round(number, 2)


def parse_choice(value, choices, field, default):
    if value in (None, ""):
        return default
    if value not in choices:
        raise ApiError(f"{field}: допустимые значения — {', '.join(choices)}")
    return value


def duty_label(kind, role, hours):
    """Например: «Официальное дежурство · Дежурный · 24 ч»."""
    return f"{DUTY_KIND_LABEL[kind]} дежурство · {ROLE_LABEL.get(role, role)} · {fmt_hours(hours)} ч"


def migrate(conn):
    conn.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                if (table, name) == ("duties", "accrued_at"):
                    # Дежурства, назначенные до появления автоначисления, часы за которые могли уже
                    # внести вручную, не начисляем задним числом — только будущие.
                    conn.execute("UPDATE duties SET accrued_at='до автоначисления' WHERE duty_date<=?",
                                 (date.today().isoformat(),))
    conn.executescript(POST_MIGRATION_SQL)
    conn.commit()


def write_audit(conn, actor, ip, action, entity, entity_id, summary, old=None, new=None):
    conn.execute(
        "INSERT INTO audit (ts, actor, ip, action, entity, entity_id, summary, old_json, new_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            now_str(), actor, ip, action, entity, entity_id, summary,
            json.dumps(old, ensure_ascii=False) if old is not None else None,
            json.dumps(new, ensure_ascii=False) if new is not None else None,
        ),
    )


def accrue_due_duties(conn, today=None):
    """Начисляет часы за дежурства, день которых наступил. Возвращает число начислений.

    Каждое дежурство начисляется один раз: отметка duties.accrued_at ставится условным UPDATE,
    так что параллельные проверки (таймер и запросы) не задвоят часы.
    """
    today = (today or date.today()).isoformat()
    due = conn.execute(
        "SELECT d.*, e.full_name FROM duties d JOIN employees e ON e.id=d.employee_id"
        " WHERE d.accrued_at IS NULL AND d.duty_date<=? ORDER BY d.duty_date, d.id",
        (today,),
    ).fetchall()
    count = 0
    for duty in due:
        ts = now_str()
        claimed = conn.execute("UPDATE duties SET accrued_at=? WHERE id=? AND accrued_at IS NULL",
                               (ts, duty["id"])).rowcount
        if not claimed:
            continue
        comment = duty_label(duty["kind"], duty["role"], duty["hours"])
        if duty["note"]:
            comment += f" · {duty['note']}"
        cur = conn.execute(
            "INSERT INTO hours (kind, work_date, employee_id, hours, comment, created_by, created_at,"
            " source, duty_id) VALUES (?,?,?,?,?,?,?, 'duty', ?)",
            (duty["kind"], duty["duty_date"], duty["employee_id"], duty["hours"], comment,
             SYSTEM_ACTOR, ts, duty["id"]),
        )
        write_audit(
            conn, SYSTEM_ACTOR, "", "create", f"hours_{duty['kind']}", cur.lastrowid,
            f"Автоначисление в «{KIND_LABEL[duty['kind']]}»: {duty['full_name']}, {duty['duty_date']}, "
            f"+{fmt_hours(duty['hours'])} ч ({comment})",
            new={"Дата": duty["duty_date"], "Сотрудник": duty["full_name"],
                 "Тип дежурства": DUTY_KIND_LABEL[duty["kind"]], "Роль": ROLE_LABEL.get(duty["role"], duty["role"]),
                 "Часы": f"+{fmt_hours(duty['hours'])}"},
        )
        count += 1
    conn.commit()
    return count


def run_accrual(db_path, today=None):
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        return accrue_due_duties(conn, today)
    finally:
        conn.close()


def create_app(db_path=None):
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False

    if db_path is None:
        data_dir = Path(os.environ.get("PERER_DATA_DIR") or BASE_DIR.parent / "data")
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "pererabotki.db"
    app.config["DB_PATH"] = str(db_path)
    trust_proxy = os.environ.get("PERER_TRUST_PROXY") == "1"

    init_db = sqlite3.connect(app.config["DB_PATH"])
    try:
        migrate(init_db)
    finally:
        init_db.close()
    # Начисление за дежурства, день которых уже наступил (при старте сервиса)
    run_accrual(app.config["DB_PATH"])
    app.accrue = lambda today=None: run_accrual(app.config["DB_PATH"], today)

    # ---------- БД ----------
    def db():
        if "db" not in g:
            conn = sqlite3.connect(app.config["DB_PATH"], timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            g.db = conn
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    # ---------- Пользователь ОС ----------
    def detect_user():
        """Возвращает (имя, источник). Источник: proxy | os | manual | unknown."""
        header = request.headers.get("X-Remote-User") or request.environ.get("REMOTE_USER")
        if trust_proxy and header:
            return header.strip(), "proxy"
        ip = request.remote_addr or ""
        if not trust_proxy and ip in ("127.0.0.1", "::1"):
            try:
                return getpass.getuser(), "os"
            except Exception:  # noqa: BLE001 — getuser может падать в урезанных окружениях
                pass
        manual = (request.cookies.get("perer_user") or "").strip()
        if manual:
            return manual[:100], "manual"
        return f"unknown@{ip}", "unknown"

    @app.before_request
    def identify():
        g.actor, g.actor_source = detect_user()
        # Ленивая проверка: если сутки сменились между проверками таймера, часы появятся сразу
        if request.path.startswith("/api/"):
            accrue_due_duties(db())

    # ---------- Аудит ----------
    def audit(action, entity, entity_id, summary, old=None, new=None):
        write_audit(db(), g.actor, request.remote_addr or "", action, entity, entity_id, summary, old, new)

    def diff(old, new):
        keys = [k for k in new if old.get(k) != new.get(k)]
        return {k: old.get(k) for k in keys}, {k: new.get(k) for k in keys}

    def changes_text(old, new):
        return "; ".join(f"{k}: «{old[k]}» → «{new[k]}»" for k in new)

    # ---------- Снимки записей для аудита ----------
    def employee_snapshot(row):
        return {"ФИО": row["full_name"], "Должность": row["position"]}

    def employee_name(employee_id):
        row = db().execute("SELECT full_name FROM employees WHERE id=?", (employee_id,)).fetchone()
        return row["full_name"] if row else f"#{employee_id}"

    def duty_snapshot(row):
        return {"Дата": row["duty_date"], "Сотрудник": employee_name(row["employee_id"]),
                "Тип дежурства": DUTY_KIND_LABEL[row["kind"]], "Роль": ROLE_LABEL.get(row["role"], row["role"]),
                "Часы": fmt_hours(row["hours"]), "Примечание": row["note"]}

    def dayoff_snapshot(row):
        return {"Дата": row["off_date"], "Сотрудник": employee_name(row["employee_id"]),
                "Списать из": KIND_LABEL[row["kind"]], "Часы": fmt_hours(row["hours"]), "Примечание": row["note"]}

    def hours_snapshot(row):
        return {"Дата": row["work_date"], "Сотрудник": employee_name(row["employee_id"]),
                "Часы": fmt_hours(row["hours"]), "Комментарий": row["comment"]}

    def linked_hours(column, ref_id):
        return db().execute(f"SELECT * FROM hours WHERE {column}=?", (ref_id,)).fetchone()

    def drop_linked_hours(column, ref_id, reason):
        """Удаляет запись часов, созданную по графику (начисление за дежурство или списание за отгул)."""
        row = linked_hours(column, ref_id)
        if not row:
            return
        snap = hours_snapshot(row)
        db().execute("DELETE FROM hours WHERE id=?", (row["id"],))
        audit("delete", f"hours_{row['kind']}", row["id"],
              f"Удалил запись из «{KIND_LABEL[row['kind']]}» ({reason}): {snap['Сотрудник']}, {snap['Дата']}, "
              f"{snap['Часы']} ч", old=snap)

    def sync_linked_hours(column, ref_id, kind, value, comment, reason):
        """Приводит запись часов по графику в соответствие с изменённым дежурством/отгулом."""
        row = linked_hours(column, ref_id)
        if not row:
            return
        before = hours_snapshot(row)
        db().execute("UPDATE hours SET kind=?, hours=?, comment=? WHERE id=?", (kind, value, comment, row["id"]))
        if kind != row["kind"]:
            # запись переехала в другую таблицу — в аудите это удаление из одной и добавление в другую
            audit("delete", f"hours_{row['kind']}", row["id"],
                  f"Перенёс запись из «{KIND_LABEL[row['kind']]}» ({reason}): {before['Сотрудник']}, "
                  f"{before['Дата']}, {before['Часы']} ч", old=before)
            after = hours_snapshot(db().execute("SELECT * FROM hours WHERE id=?", (row["id"],)).fetchone())
            audit("create", f"hours_{kind}", row["id"],
                  f"Перенёс запись в «{KIND_LABEL[kind]}» ({reason}): {after['Сотрудник']}, "
                  f"{after['Дата']}, {after['Часы']} ч", new=after)
            return
        after = hours_snapshot(db().execute("SELECT * FROM hours WHERE id=?", (row["id"],)).fetchone())
        old, new = diff(before, after)
        if new:
            audit("update", f"hours_{kind}", row["id"],
                  f"Изменил запись в «{KIND_LABEL[kind]}» ({reason}, {before['Сотрудник']}, {before['Дата']}): "
                  f"{changes_text(old, new)}", old, new)

    # ---------- Ошибки ----------
    @app.errorhandler(ApiError)
    def handle_api_error(err):
        return jsonify({"error": err.message}), err.status

    @app.errorhandler(404)
    def handle_404(_err):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Не найдено"}), 404
        return Response("Не найдено", status=404)

    @app.errorhandler(405)
    def handle_405(_err):
        return jsonify({"error": "Метод не поддерживается"}), 405

    # ---------- Статика ----------
    @app.get("/")
    def index():
        resp = send_from_directory(STATIC_DIR, "index.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/static/<path:name>")
    def static_files(name):
        resp = send_from_directory(STATIC_DIR, name)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ---------- Кто я ----------
    @app.get("/api/me")
    def me():
        return jsonify({"user": g.actor, "source": g.actor_source})

    @app.post("/api/me")
    def set_me():
        """Запасной вариант: имя вводится вручную, если ОС-учетку определить нельзя."""
        name = clean_text((request.get_json(silent=True) or {}).get("name"), "Имя", True, 100)
        resp = jsonify({"user": name, "source": "manual"})
        resp.set_cookie("perer_user", name, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp

    # ---------- Дежурные (сотрудники) ----------
    @app.get("/api/employees")
    def list_employees():
        sql = "SELECT * FROM employees"
        if request.args.get("all") != "1":
            sql += " WHERE active=1"
        rows = db().execute(sql + " ORDER BY full_name COLLATE NOCASE").fetchall()
        return jsonify([dict(r) for r in rows])

    def employee_payload():
        data = request.get_json(silent=True) or {}
        return (clean_text(data.get("full_name"), "ФИО", True, 200),
                clean_text(data.get("position"), "Должность", True, 200))

    def name_taken(full_name, exclude_id=None):
        # lower() в SQLite не понимает кириллицу, поэтому сравниваем в Python
        key = full_name.casefold()
        rows = db().execute("SELECT id, full_name FROM employees WHERE active=1").fetchall()
        return any(r["full_name"].casefold() == key and r["id"] != exclude_id for r in rows)

    @app.post("/api/employees")
    def add_employee():
        full_name, position = employee_payload()
        if name_taken(full_name):
            raise ApiError("Дежурный с таким ФИО уже есть в списке", 409)
        cur = db().execute(
            "INSERT INTO employees (full_name, position, created_at) VALUES (?,?,?)",
            (full_name, position, now_str()),
        )
        row = db().execute("SELECT * FROM employees WHERE id=?", (cur.lastrowid,)).fetchone()
        audit("create", "employee", row["id"],
              f"Добавил дежурного: {full_name} ({position})", new=employee_snapshot(row))
        db().commit()
        return jsonify(dict(row)), 201

    def get_employee(employee_id):
        row = db().execute("SELECT * FROM employees WHERE id=? AND active=1", (employee_id,)).fetchone()
        if not row:
            raise ApiError("Дежурный не найден", 404)
        return row

    @app.put("/api/employees/<int:employee_id>")
    def update_employee(employee_id):
        row = get_employee(employee_id)
        full_name, position = employee_payload()
        if name_taken(full_name, exclude_id=employee_id):
            raise ApiError("Дежурный с таким ФИО уже есть в списке", 409)
        old, new = diff(employee_snapshot(row), {"ФИО": full_name, "Должность": position})
        if new:
            db().execute("UPDATE employees SET full_name=?, position=? WHERE id=?",
                         (full_name, position, employee_id))
            audit("update", "employee", employee_id,
                  f"Изменил данные дежурного {row['full_name']}: {changes_text(old, new)}", old, new)
            db().commit()
        return jsonify(dict(db().execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()))

    @app.delete("/api/employees/<int:employee_id>")
    def delete_employee(employee_id):
        """Мягкое удаление: история часов и аудита сохраняется, из списков человек исчезает."""
        row = get_employee(employee_id)
        today = date.today().isoformat()
        # Сегодняшнее дежурство уже начислено и считается состоявшимся, снимаются только будущие
        future = db().execute(
            "SELECT * FROM duties WHERE employee_id=? AND duty_date>?", (employee_id, today)
        ).fetchall()
        for duty in future:
            drop_linked_hours("duty_id", duty["id"], "дежурство снято")
            audit("delete", "duty", duty["id"],
                  f"Снял с дежурства {duty['duty_date']}: {row['full_name']} (дежурный удалён из списка)",
                  old=duty_snapshot(duty))
        db().execute("DELETE FROM duties WHERE employee_id=? AND duty_date>?", (employee_id, today))
        future_off = db().execute(
            "SELECT * FROM dayoffs WHERE employee_id=? AND off_date>?", (employee_id, today)
        ).fetchall()
        for off in future_off:
            drop_linked_hours("dayoff_id", off["id"], "отгул отменён")
            audit("delete", "dayoff", off["id"],
                  f"Отменил отгул {off['off_date']}: {row['full_name']} (дежурный удалён из списка)",
                  old=dayoff_snapshot(off))
        db().execute("DELETE FROM dayoffs WHERE employee_id=? AND off_date>?", (employee_id, today))
        db().execute("UPDATE employees SET active=0 WHERE id=?", (employee_id,))
        audit("delete", "employee", employee_id,
              f"Удалил дежурного: {row['full_name']} ({row['position']})",
              old=employee_snapshot(row))
        db().commit()
        return jsonify({"ok": True, "removed_future_duties": len(future), "removed_future_dayoffs": len(future_off)})

    # ---------- График дежурств ----------
    @app.get("/api/duties")
    def list_duties():
        month = parse_month(request.args.get("month"))
        rows = db().execute(
            "SELECT d.id, d.duty_date, d.employee_id, d.note, d.kind, d.hours, d.role, d.accrued_at,"
            " e.full_name, e.position"
            " FROM duties d JOIN employees e ON e.id=d.employee_id"
            " WHERE substr(d.duty_date,1,7)=? ORDER BY d.duty_date, e.full_name COLLATE NOCASE",
            (month,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    def duty_payload(data):
        return (parse_choice(data.get("kind"), DUTY_KIND_LABEL, "Тип дежурства", "official"),
                parse_hours(DEFAULT_DUTY_HOURS if data.get("hours") in (None, "") else data.get("hours")),
                parse_choice(data.get("role"), ROLE_LABEL, "Роль", "duty"),
                clean_text(data.get("note"), "Примечание"))

    @app.post("/api/duties")
    def add_duty():
        data = request.get_json(silent=True) or {}
        duty_date = parse_date(data.get("date"))
        employee = get_employee(data.get("employee_id") if isinstance(data.get("employee_id"), int) else -1)
        kind, value, role, note = duty_payload(data)
        try:
            cur = db().execute(
                "INSERT INTO duties (duty_date, employee_id, note, kind, hours, role) VALUES (?,?,?,?,?,?)",
                (duty_date, employee["id"], note, kind, value, role),
            )
        except sqlite3.IntegrityError:
            raise ApiError("Этот дежурный уже назначен на выбранную дату", 409)
        row = db().execute("SELECT * FROM duties WHERE id=?", (cur.lastrowid,)).fetchone()
        audit("create", "duty", row["id"],
              f"Назначил дежурство {duty_date}: {employee['full_name']} ({duty_label(kind, role, value)})",
              new=duty_snapshot(row))
        db().commit()
        accrue_due_duties(db())  # если день уже наступил — начислить сразу
        return jsonify(dict(db().execute("SELECT * FROM duties WHERE id=?", (row["id"],)).fetchone())), 201

    def get_duty(duty_id):
        row = db().execute("SELECT * FROM duties WHERE id=?", (duty_id,)).fetchone()
        if not row:
            raise ApiError("Дежурство не найдено", 404)
        return row

    @app.put("/api/duties/<int:duty_id>")
    def update_duty(duty_id):
        row = get_duty(duty_id)
        kind, value, role, note = duty_payload(request.get_json(silent=True) or {})
        before = duty_snapshot(row)
        db().execute("UPDATE duties SET kind=?, hours=?, role=?, note=? WHERE id=?", (kind, value, role, note, duty_id))
        old, new = diff(before, duty_snapshot(get_duty(duty_id)))
        if new:
            audit("update", "duty", duty_id,
                  f"Изменил дежурство {row['duty_date']} ({before['Сотрудник']}): {changes_text(old, new)}", old, new)
            comment = duty_label(kind, role, value) + (f" · {note}" if note else "")
            sync_linked_hours("duty_id", duty_id, kind, value, comment, "дежурство изменено")
            db().commit()
        return jsonify(dict(get_duty(duty_id)))

    @app.delete("/api/duties/<int:duty_id>")
    def delete_duty(duty_id):
        row = get_duty(duty_id)
        snap = duty_snapshot(row)
        drop_linked_hours("duty_id", duty_id, "дежурство снято")
        db().execute("DELETE FROM duties WHERE id=?", (duty_id,))
        audit("delete", "duty", duty_id,
              f"Снял с дежурства {row['duty_date']}: {snap['Сотрудник']}", old=snap)
        db().commit()
        return jsonify({"ok": True})

    # ---------- График отгулов ----------
    @app.get("/api/dayoffs")
    def list_dayoffs():
        month = parse_month(request.args.get("month"))
        rows = db().execute(
            "SELECT o.id, o.off_date, o.employee_id, o.kind, o.hours, o.note, o.created_by,"
            " e.full_name, e.position FROM dayoffs o JOIN employees e ON e.id=o.employee_id"
            " WHERE substr(o.off_date,1,7)=? ORDER BY o.off_date, e.full_name COLLATE NOCASE",
            (month,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    def dayoff_payload(data):
        return (parse_choice(data.get("kind"), KIND_LABEL, "Тип часов", "official"),
                parse_hours(DEFAULT_DAYOFF_HOURS if data.get("hours") in (None, "") else data.get("hours")),
                clean_text(data.get("note"), "Примечание"))

    def dayoff_comment(value, note):
        return f"Отгул · списано {fmt_hours(value)} ч" + (f" · {note}" if note else "")

    def get_dayoff(dayoff_id):
        row = db().execute("SELECT * FROM dayoffs WHERE id=?", (dayoff_id,)).fetchone()
        if not row:
            raise ApiError("Отгул не найден", 404)
        return row

    @app.post("/api/dayoffs")
    def add_dayoff():
        data = request.get_json(silent=True) or {}
        off_date = parse_date(data.get("date"))
        employee = get_employee(data.get("employee_id") if isinstance(data.get("employee_id"), int) else -1)
        kind, value, note = dayoff_payload(data)
        ts = now_str()
        try:
            cur = db().execute(
                "INSERT INTO dayoffs (off_date, employee_id, kind, hours, note, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (off_date, employee["id"], kind, value, note, g.actor, ts),
            )
        except sqlite3.IntegrityError:
            raise ApiError("У этого сотрудника уже есть отгул на выбранную дату", 409)
        row = get_dayoff(cur.lastrowid)
        audit("create", "dayoff", row["id"],
              f"Поставил отгул {off_date}: {employee['full_name']}, списать {fmt_hours(value)} ч из «{KIND_LABEL[kind]}»",
              new=dayoff_snapshot(row))
        # Списание: отрицательная запись в соответствующей таблице часов
        hcur = db().execute(
            "INSERT INTO hours (kind, work_date, employee_id, hours, comment, created_by, created_at, source, dayoff_id)"
            " VALUES (?,?,?,?,?,?,?, 'dayoff', ?)",
            (kind, off_date, employee["id"], -value, dayoff_comment(value, note), g.actor, ts, row["id"]),
        )
        hrow = db().execute("SELECT * FROM hours WHERE id=?", (hcur.lastrowid,)).fetchone()
        audit("create", f"hours_{kind}", hrow["id"],
              f"Списал за отгул из «{KIND_LABEL[kind]}»: {employee['full_name']}, {off_date}, −{fmt_hours(value)} ч",
              new=hours_snapshot(hrow))
        db().commit()
        return jsonify(dict(row)), 201

    @app.put("/api/dayoffs/<int:dayoff_id>")
    def update_dayoff(dayoff_id):
        row = get_dayoff(dayoff_id)
        kind, value, note = dayoff_payload(request.get_json(silent=True) or {})
        before = dayoff_snapshot(row)
        db().execute("UPDATE dayoffs SET kind=?, hours=?, note=? WHERE id=?", (kind, value, note, dayoff_id))
        old, new = diff(before, dayoff_snapshot(get_dayoff(dayoff_id)))
        if new:
            audit("update", "dayoff", dayoff_id,
                  f"Изменил отгул {row['off_date']} ({before['Сотрудник']}): {changes_text(old, new)}", old, new)
            sync_linked_hours("dayoff_id", dayoff_id, kind, -value, dayoff_comment(value, note), "отгул изменён")
            db().commit()
        return jsonify(dict(get_dayoff(dayoff_id)))

    @app.delete("/api/dayoffs/<int:dayoff_id>")
    def delete_dayoff(dayoff_id):
        row = get_dayoff(dayoff_id)
        snap = dayoff_snapshot(row)
        drop_linked_hours("dayoff_id", dayoff_id, "отгул отменён, часы возвращены")
        db().execute("DELETE FROM dayoffs WHERE id=?", (dayoff_id,))
        audit("delete", "dayoff", dayoff_id, f"Отменил отгул {row['off_date']}: {snap['Сотрудник']}", old=snap)
        db().commit()
        return jsonify({"ok": True})

    @app.get("/api/balance")
    def balance():
        """Остаток часов за всё время: начислено минус списано, по каждому активному сотруднику."""
        rows = db().execute(
            "SELECT e.id AS employee_id, e.full_name, e.position,"
            " COALESCE(SUM(CASE WHEN h.kind='official' THEN h.hours END), 0) AS official,"
            " COALESCE(SUM(CASE WHEN h.kind='unofficial' THEN h.hours END), 0) AS unofficial"
            " FROM employees e LEFT JOIN hours h ON h.employee_id=e.id"
            " WHERE e.active=1 GROUP BY e.id ORDER BY e.full_name COLLATE NOCASE"
        ).fetchall()
        return jsonify([dict(r, official=round(r["official"], 2), unofficial=round(r["unofficial"], 2)) for r in rows])

    # ---------- Часы переработки ----------
    def check_kind(kind):
        if kind not in KIND_LABEL:
            raise ApiError("Неизвестный тип часов", 404)
        return kind

    def hours_payload():
        data = request.get_json(silent=True) or {}
        work_date = parse_date(data.get("date"))
        value = parse_hours(data.get("hours"))
        employee_id = data.get("employee_id")
        if not isinstance(employee_id, int):
            raise ApiError("Выберите сотрудника")
        return work_date, employee_id, value, clean_text(data.get("comment"), "Комментарий", False, 500)

    @app.get("/api/hours/summary")
    def hours_summary():
        month = parse_month(request.args.get("month"))
        rows = db().execute(
            "SELECT e.id AS employee_id, e.full_name, e.position,"
            " SUM(CASE WHEN h.kind='official' THEN h.hours ELSE 0 END) AS official,"
            " SUM(CASE WHEN h.kind='unofficial' THEN h.hours ELSE 0 END) AS unofficial"
            " FROM hours h JOIN employees e ON e.id=h.employee_id"
            " WHERE substr(h.work_date,1,7)=? GROUP BY e.id ORDER BY e.full_name COLLATE NOCASE",
            (month,),
        ).fetchall()
        result = [dict(r, total=round(r["official"] + r["unofficial"], 2)) for r in rows]
        return jsonify(result)

    @app.get("/api/hours/<kind>")
    def list_hours(kind):
        check_kind(kind)
        month = parse_month(request.args.get("month"))
        sql = ("SELECT h.id, h.work_date, h.employee_id, h.hours, h.comment, h.created_by, h.source,"
               " h.duty_id, h.dayoff_id,"
               " e.full_name, e.position FROM hours h JOIN employees e ON e.id=h.employee_id"
               " WHERE h.kind=? AND substr(h.work_date,1,7)=?")
        params = [kind, month]
        if request.args.get("employee_id", "").isdigit():
            sql += " AND h.employee_id=?"
            params.append(int(request.args["employee_id"]))
        rows = db().execute(sql + " ORDER BY h.work_date, e.full_name COLLATE NOCASE, h.id", params).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.post("/api/hours/<kind>")
    def add_hours(kind):
        check_kind(kind)
        work_date, employee_id, value, comment = hours_payload()
        employee = get_employee(employee_id)
        cur = db().execute(
            "INSERT INTO hours (kind, work_date, employee_id, hours, comment, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (kind, work_date, employee_id, value, comment, g.actor, now_str()),
        )
        row = db().execute("SELECT * FROM hours WHERE id=?", (cur.lastrowid,)).fetchone()
        audit("create", f"hours_{kind}", row["id"],
              f"Добавил запись в «{KIND_LABEL[kind]}»: {employee['full_name']}, {work_date}, {fmt_hours(value)} ч",
              new=hours_snapshot(row))
        db().commit()
        return jsonify(dict(row)), 201

    def get_hours_row(kind, hours_id, manual_only=False):
        row = db().execute("SELECT * FROM hours WHERE id=? AND kind=?", (hours_id, kind)).fetchone()
        if not row:
            raise ApiError("Запись не найдена", 404)
        if manual_only and row["source"] != "manual":
            where = "графике дежурств" if row["source"] == "duty" else "графике отгулов"
            raise ApiError(f"Запись создана автоматически — измените или удалите её в {where}", 409)
        return row

    @app.put("/api/hours/<kind>/<int:hours_id>")
    def update_hours(kind, hours_id):
        check_kind(kind)
        row = get_hours_row(kind, hours_id, manual_only=True)
        work_date, employee_id, value, comment = hours_payload()
        if employee_id != row["employee_id"]:
            get_employee(employee_id)
        before = hours_snapshot(row)
        db().execute(
            "UPDATE hours SET work_date=?, employee_id=?, hours=?, comment=? WHERE id=?",
            (work_date, employee_id, value, comment, hours_id),
        )
        updated = db().execute("SELECT * FROM hours WHERE id=?", (hours_id,)).fetchone()
        old, new = diff(before, hours_snapshot(updated))
        if new:
            audit("update", f"hours_{kind}", hours_id,
                  f"Изменил запись в «{KIND_LABEL[kind]}» ({before['Сотрудник']}, {before['Дата']}): "
                  f"{changes_text(old, new)}", old, new)
        db().commit()
        return jsonify(dict(updated))

    @app.delete("/api/hours/<kind>/<int:hours_id>")
    def delete_hours(kind, hours_id):
        check_kind(kind)
        row = get_hours_row(kind, hours_id, manual_only=True)
        snap = hours_snapshot(row)
        db().execute("DELETE FROM hours WHERE id=?", (hours_id,))
        audit("delete", f"hours_{kind}", hours_id,
              f"Удалил запись из «{KIND_LABEL[kind]}»: {snap['Сотрудник']}, {snap['Дата']}, {snap['Часы']} ч",
              old=snap)
        db().commit()
        return jsonify({"ok": True})

    # ---------- Месяцы с данными ----------
    @app.get("/api/months")
    def months():
        rows = db().execute(
            "SELECT m FROM (SELECT substr(duty_date,1,7) AS m FROM duties"
            " UNION SELECT substr(work_date,1,7) FROM hours"
            " UNION SELECT substr(off_date,1,7) FROM dayoffs) ORDER BY m DESC"
        ).fetchall()
        found = {r["m"] for r in rows}
        found.add(date.today().strftime("%Y-%m"))
        return jsonify(sorted(found, reverse=True))

    # ---------- Аудит ----------
    @app.get("/api/audit")
    def list_audit():
        sql, params = "SELECT * FROM audit WHERE 1=1", []
        if request.args.get("entity"):
            sql += " AND entity=?"
            params.append(request.args["entity"])
        if request.args.get("actor"):
            sql += " AND actor LIKE ?"
            params.append(f"%{request.args['actor']}%")
        if request.args.get("q"):
            sql += " AND summary LIKE ?"
            params.append(f"%{request.args['q']}%")
        try:
            limit = min(max(int(request.args.get("limit", 50)), 1), 500)
            offset = max(int(request.args.get("offset", 0)), 0)
        except ValueError:
            raise ApiError("limit/offset: ожидаются числа")
        total = db().execute(sql.replace("SELECT *", "SELECT COUNT(*)", 1), params).fetchone()[0]
        rows = db().execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            old_json, new_json = item.pop("old_json"), item.pop("new_json")
            item["old"] = json.loads(old_json) if old_json else None
            item["new"] = json.loads(new_json) if new_json else None
            item["entity_label"] = ENTITY_LABEL.get(r["entity"], r["entity"])
            items.append(item)
        return jsonify({"total": total, "items": items})

    return app


def setup_logging(data_dir):
    handler = RotatingFileHandler(Path(data_dir) / "server.log", maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    # pythonw (автозапуск в Windows) не имеет stdout/stderr
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def start_accrual_timer(app, interval):
    """Фоновая периодическая проверка: начисляет часы, когда наступает день дежурства."""
    stop = threading.Event()

    def loop():
        while not stop.wait(max(interval, 10)):
            try:
                count = app.accrue()
                if count:
                    logging.info("Начислены часы за дежурства: %s", count)
            except Exception:  # noqa: BLE001 — таймер не должен падать из-за одной ошибки
                logging.exception("Ошибка автоначисления часов")

    threading.Thread(target=loop, name="accrual", daemon=True).start()
    return stop


def main():
    host = os.environ.get("PERER_HOST", "127.0.0.1")
    port = int(os.environ.get("PERER_PORT", "8080"))
    data_dir = Path(os.environ.get("PERER_DATA_DIR") or BASE_DIR.parent / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(data_dir)
    app = create_app()
    start_accrual_timer(app, int(os.environ.get("PERER_ACCRUE_INTERVAL", "300")))
    logging.info("Запуск на http://%s:%s", host, port)
    try:
        from waitress import serve
    except ImportError:
        app.run(host=host, port=port, threaded=True)
    else:
        serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
