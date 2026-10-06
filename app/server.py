"""Переработка — веб-сервис учета часов переработки.

Flask + SQLite. Запуск: python app/server.py
Настройки через переменные окружения:
  PERER_HOST         адрес прослушивания (по умолчанию 127.0.0.1)
  PERER_PORT         порт (по умолчанию 8080)
  PERER_DATA_DIR     папка с базой и логом (по умолчанию ./data рядом с проектом)
  PERER_TRUST_PROXY  1 — доверять заголовку X-Remote-User от обратного прокси (SSO)
"""
import getpass
import json
import logging
import os
import re
import sqlite3
import sys
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
"""

KIND_LABEL = {"official": "Официальные часы", "unofficial": "Неофициальные часы"}
ENTITY_LABEL = {
    "employee": "Список дежурных",
    "duty": "График дежурств",
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
        init_db.executescript(SCHEMA)
    finally:
        init_db.close()

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

    # ---------- Аудит ----------
    def audit(action, entity, entity_id, summary, old=None, new=None):
        db().execute(
            "INSERT INTO audit (ts, actor, ip, action, entity, entity_id, summary, old_json, new_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                now_str(), g.actor, request.remote_addr or "", action, entity, entity_id, summary,
                json.dumps(old, ensure_ascii=False) if old is not None else None,
                json.dumps(new, ensure_ascii=False) if new is not None else None,
            ),
        )

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
                "Примечание": row["note"]}

    def hours_snapshot(row):
        return {"Дата": row["work_date"], "Сотрудник": employee_name(row["employee_id"]),
                "Часы": fmt_hours(row["hours"]), "Комментарий": row["comment"]}

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
        future = db().execute(
            "SELECT * FROM duties WHERE employee_id=? AND duty_date>=?", (employee_id, today)
        ).fetchall()
        for duty in future:
            audit("delete", "duty", duty["id"],
                  f"Снял с дежурства {duty['duty_date']}: {row['full_name']} (дежурный удалён из списка)",
                  old=duty_snapshot(duty))
        db().execute("DELETE FROM duties WHERE employee_id=? AND duty_date>=?", (employee_id, today))
        db().execute("UPDATE employees SET active=0 WHERE id=?", (employee_id,))
        audit("delete", "employee", employee_id,
              f"Удалил дежурного: {row['full_name']} ({row['position']})",
              old=employee_snapshot(row))
        db().commit()
        return jsonify({"ok": True, "removed_future_duties": len(future)})

    # ---------- График дежурств ----------
    @app.get("/api/duties")
    def list_duties():
        month = parse_month(request.args.get("month"))
        rows = db().execute(
            "SELECT d.id, d.duty_date, d.employee_id, d.note, e.full_name, e.position"
            " FROM duties d JOIN employees e ON e.id=d.employee_id"
            " WHERE substr(d.duty_date,1,7)=? ORDER BY d.duty_date, e.full_name COLLATE NOCASE",
            (month,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.post("/api/duties")
    def add_duty():
        data = request.get_json(silent=True) or {}
        duty_date = parse_date(data.get("date"))
        employee = get_employee(data.get("employee_id") if isinstance(data.get("employee_id"), int) else -1)
        note = clean_text(data.get("note"), "Примечание")
        try:
            cur = db().execute(
                "INSERT INTO duties (duty_date, employee_id, note) VALUES (?,?,?)",
                (duty_date, employee["id"], note),
            )
        except sqlite3.IntegrityError:
            raise ApiError("Этот дежурный уже назначен на выбранную дату", 409)
        row = db().execute("SELECT * FROM duties WHERE id=?", (cur.lastrowid,)).fetchone()
        audit("create", "duty", row["id"],
              f"Назначил дежурство {duty_date}: {employee['full_name']}", new=duty_snapshot(row))
        db().commit()
        return jsonify(dict(row)), 201

    @app.delete("/api/duties/<int:duty_id>")
    def delete_duty(duty_id):
        row = db().execute("SELECT * FROM duties WHERE id=?", (duty_id,)).fetchone()
        if not row:
            raise ApiError("Дежурство не найдено", 404)
        snap = duty_snapshot(row)
        db().execute("DELETE FROM duties WHERE id=?", (duty_id,))
        audit("delete", "duty", duty_id,
              f"Снял с дежурства {row['duty_date']}: {snap['Сотрудник']}", old=snap)
        db().commit()
        return jsonify({"ok": True})

    # ---------- Часы переработки ----------
    def check_kind(kind):
        if kind not in KIND_LABEL:
            raise ApiError("Неизвестный тип часов", 404)
        return kind

    def hours_payload():
        data = request.get_json(silent=True) or {}
        work_date = parse_date(data.get("date"))
        try:
            value = float(data.get("hours"))
        except (TypeError, ValueError):
            raise ApiError("Часы: ожидается число")
        if not 0 < value <= 24:
            raise ApiError("Часы: допустимо значение больше 0 и не более 24")
        value = round(value, 2)
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
        sql = ("SELECT h.id, h.work_date, h.employee_id, h.hours, h.comment, h.created_by,"
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

    def get_hours_row(kind, hours_id):
        row = db().execute("SELECT * FROM hours WHERE id=? AND kind=?", (hours_id, kind)).fetchone()
        if not row:
            raise ApiError("Запись не найдена", 404)
        return row

    @app.put("/api/hours/<kind>/<int:hours_id>")
    def update_hours(kind, hours_id):
        check_kind(kind)
        row = get_hours_row(kind, hours_id)
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
        row = get_hours_row(kind, hours_id)
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
            " UNION SELECT substr(work_date,1,7) FROM hours) ORDER BY m DESC"
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


def main():
    host = os.environ.get("PERER_HOST", "127.0.0.1")
    port = int(os.environ.get("PERER_PORT", "8080"))
    data_dir = Path(os.environ.get("PERER_DATA_DIR") or BASE_DIR.parent / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(data_dir)
    app = create_app()
    logging.info("Запуск на http://%s:%s", host, port)
    try:
        from waitress import serve
    except ImportError:
        app.run(host=host, port=port, threaded=True)
    else:
        serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
