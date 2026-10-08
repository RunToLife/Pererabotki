import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import winauth  # noqa: E402
from app.server import create_app  # noqa: E402


class FakeAuthenticator:
    """Имитирует двухшаговое рукопожатие NTLM без Windows."""

    def __init__(self):
        self.pending = set()

    def step(self, key, scheme, token):
        if token == b"type1":
            self.pending.add(key)
            return winauth.AuthResult(b"challenge")
        if token == b"type3" and key in self.pending:
            self.pending.discard(key)
            return winauth.AuthResult(user="CORP\\ivanov")
        return winauth.AuthResult(failed=True)


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(os.path.join(self.tmp.name, "test.db"))
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def emp(self, name="Иванов Иван Иванович", pos="Инженер"):
        r = self.c.post("/api/employees", json={"full_name": name, "position": pos})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["id"]

    def audit(self, **params):
        return self.c.get("/api/audit", query_string=params).get_json()

    # --- пользователь ---
    def test_me_local_uses_os_user(self):
        r = self.c.get("/api/me", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).get_json()
        self.assertEqual(r["source"], "os")
        self.assertTrue(r["user"])

    def test_me_remote_unknown_then_manual(self):
        remote = {"REMOTE_ADDR": "10.0.0.5"}
        self.assertEqual(self.c.get("/api/me", environ_overrides=remote).get_json()["source"], "unknown")
        self.c.post("/api/me", json={"name": "petrov"}, environ_overrides=remote)
        r = self.c.get("/api/me", environ_overrides=remote).get_json()
        self.assertEqual((r["user"], r["source"]), ("petrov", "manual"))

    def test_proxy_header_ignored_without_trust(self):
        r = self.c.get("/api/me", headers={"X-Remote-User": "evil"},
                       environ_overrides={"REMOTE_ADDR": "10.0.0.5"}).get_json()
        self.assertNotEqual(r["user"], "evil")

    def test_proxy_header_trusted(self):
        os.environ["PERER_TRUST_PROXY"] = "1"
        try:
            app = create_app(os.path.join(self.tmp.name, "proxy.db"))
        finally:
            del os.environ["PERER_TRUST_PROXY"]
        r = app.test_client().get("/api/me", headers={"X-Remote-User": "DOM\\sidorov"}).get_json()
        self.assertEqual((r["user"], r["source"]), ("DOM\\sidorov", "proxy"))

    # --- дежурные ---
    def test_employee_crud_and_validation(self):
        eid = self.emp()
        self.assertEqual(self.c.post("/api/employees", json={"full_name": "иванов иван иванович",
                                                              "position": "X"}).status_code, 409)
        self.assertEqual(self.c.post("/api/employees", json={"full_name": " ", "position": "X"}).status_code, 400)
        r = self.c.put(f"/api/employees/{eid}", json={"full_name": "Иванов И. И.", "position": "Старший инженер"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.delete(f"/api/employees/{eid}").status_code, 200)
        self.assertEqual(self.c.get("/api/employees").get_json(), [])
        self.assertEqual(len(self.c.get("/api/employees?all=1").get_json()), 1)
        self.assertEqual(self.c.delete(f"/api/employees/{eid}").status_code, 404)

    # --- график ---
    def test_duties_by_month_and_conflicts(self):
        eid = self.emp()
        r = self.c.post("/api/duties", json={"date": "2026-09-15", "employee_id": eid, "note": "ночь"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.c.post("/api/duties", json={"date": "2026-09-15", "employee_id": eid}).status_code, 409)
        self.assertEqual(self.c.post("/api/duties", json={"date": "2026-13-45", "employee_id": eid}).status_code, 400)
        self.assertEqual(self.c.post("/api/duties", json={"date": "2026-09-16", "employee_id": 999}).status_code, 404)
        self.assertEqual(len(self.c.get("/api/duties?month=2026-09").get_json()), 1)
        self.assertEqual(self.c.get("/api/duties?month=2026-08").get_json(), [])
        self.assertEqual(self.c.get("/api/duties?month=bad").status_code, 400)
        self.assertIn("2026-09", self.c.get("/api/months").get_json())
        self.assertEqual(self.c.delete(f"/api/duties/{r.get_json()['id']}").status_code, 200)

    def test_deleting_employee_removes_only_future_duties(self):
        eid = self.emp()
        past = (date.today() - timedelta(days=5)).isoformat()
        future = (date.today() + timedelta(days=5)).isoformat()
        self.c.post("/api/duties", json={"date": past, "employee_id": eid})
        self.c.post("/api/duties", json={"date": future, "employee_id": eid})
        r = self.c.delete(f"/api/employees/{eid}").get_json()
        self.assertEqual(r["removed_future_duties"], 1)
        month = past[:7]
        duties = self.c.get(f"/api/duties?month={month}").get_json()
        self.assertTrue(any(d["duty_date"] == past for d in duties))
        self.assertFalse(any(d["duty_date"] == future for d in duties))

    # --- часы ---
    def test_hours_two_kinds_and_summary(self):
        eid = self.emp()
        for kind, h in (("official", 2), ("official", 1.5), ("unofficial", 3)):
            r = self.c.post(f"/api/hours/{kind}", json={"date": "2026-09-03", "employee_id": eid, "hours": h})
            self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(len(self.c.get("/api/hours/official?month=2026-09").get_json()), 2)
        self.assertEqual(len(self.c.get("/api/hours/unofficial?month=2026-09").get_json()), 1)
        s = self.c.get("/api/hours/summary?month=2026-09").get_json()[0]
        self.assertEqual((s["official"], s["unofficial"], s["total"]), (3.5, 3, 6.5))
        self.assertEqual(self.c.get("/api/hours/other?month=2026-09").status_code, 404)

    def test_hours_validation(self):
        eid = self.emp()
        url = "/api/hours/official"
        for payload in ({"hours": 0}, {"hours": 25}, {"hours": "abc"}, {"hours": -1}, {}):
            body = {"date": "2026-09-03", "employee_id": eid, **payload}
            self.assertEqual(self.c.post(url, json=body).status_code, 400, payload)
        self.assertEqual(self.c.post(url, json={"date": "bad", "employee_id": eid, "hours": 1}).status_code, 400)

    def test_hours_update_delete_kind_isolation(self):
        eid = self.emp()
        hid = self.c.post("/api/hours/official", json={"date": "2026-09-03", "employee_id": eid,
                                                       "hours": 2}).get_json()["id"]
        body = {"date": "2026-09-04", "employee_id": eid, "hours": 4, "comment": "аврал"}
        self.assertEqual(self.c.put(f"/api/hours/unofficial/{hid}", json=body).status_code, 404)
        self.assertEqual(self.c.put(f"/api/hours/official/{hid}", json=body).status_code, 200)
        self.assertEqual(self.c.delete(f"/api/hours/official/{hid}").status_code, 200)
        self.assertEqual(self.c.get("/api/hours/official?month=2026-09").get_json(), [])

    # --- аудит ---
    def test_audit_records_who_what_old_new(self):
        eid = self.emp()
        hid = self.c.post("/api/hours/official", json={"date": "2026-09-03", "employee_id": eid,
                                                       "hours": 2}).get_json()["id"]
        self.c.put(f"/api/hours/official/{hid}", json={"date": "2026-09-03", "employee_id": eid,
                                                       "hours": 5, "comment": ""})
        self.c.delete(f"/api/hours/official/{hid}")
        data = self.audit(entity="hours_official")
        self.assertEqual(data["total"], 3)
        delete, update, create = data["items"]
        self.assertEqual((create["action"], update["action"], delete["action"]), ("create", "update", "delete"))
        self.assertEqual(update["old"], {"Часы": "2"})
        self.assertEqual(update["new"], {"Часы": "5"})
        self.assertEqual(update["entity_label"], "Официальные часы")
        self.assertTrue(update["actor"])
        self.assertIn("«2» → «5»", update["summary"])
        self.assertEqual(delete["old"]["Часы"], "5")

    def test_audit_filters_and_no_entry_for_noop_update(self):
        eid = self.emp()
        before = self.audit()["total"]
        self.c.put(f"/api/employees/{eid}", json={"full_name": "Иванов Иван Иванович", "position": "Инженер"})
        self.assertEqual(self.audit()["total"], before)
        self.c.put(f"/api/employees/{eid}", json={"full_name": "Иванов Иван Иванович", "position": "Директор"})
        self.assertEqual(self.audit(entity="employee", q="Директор")["total"], 1)
        self.assertEqual(self.audit(actor="zzz-nobody")["total"], 0)
        self.assertEqual(len(self.audit(limit=1)["items"]), 1)

    # --- типы дежурств и автоначисление ---
    def hours(self, kind, month):
        return self.c.get(f"/api/hours/{kind}?month={month}").get_json()

    def test_duty_defaults_and_validation(self):
        eid = self.emp()
        future = (date.today() + timedelta(days=3)).isoformat()
        d = self.c.post("/api/duties", json={"date": future, "employee_id": eid}).get_json()
        self.assertEqual((d["kind"], d["hours"], d["role"], d["accrued_at"]), ("official", 24, "duty", None))
        for bad in ({"kind": "x"}, {"role": "boss"}, {"hours": 0}, {"hours": 30}):
            body = {"date": (date.today() + timedelta(days=4)).isoformat(), "employee_id": eid, **bad}
            self.assertEqual(self.c.post("/api/duties", json=body).status_code, 400, bad)
        # будущее дежурство ещё не начислено
        self.assertEqual(self.hours("official", future[:7]), [])

    def test_duty_accrues_on_its_day_once(self):
        eid = self.emp()
        future = date.today() + timedelta(days=2)
        d = self.c.post("/api/duties", json={"date": future.isoformat(), "employee_id": eid,
                                             "kind": "unofficial", "hours": 12, "role": "assistant"}).get_json()
        self.assertEqual(self.app.accrue(date.today()), 0)
        self.assertEqual(self.app.accrue(future), 1)
        self.assertEqual(self.app.accrue(future), 0)  # повторная проверка не задваивает
        rows = self.hours("unofficial", future.isoformat()[:7])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["hours"], rows[0]["source"], rows[0]["duty_id"]), (12, "duty", d["id"]))
        self.assertIn("Неофициальное дежурство · Помощник дежурного · 12 ч", rows[0]["comment"])
        self.assertEqual(self.hours("official", future.isoformat()[:7]), [])
        a = self.audit(entity="hours_unofficial")["items"][0]
        self.assertEqual(a["actor"], "система")
        self.assertIn("+12 ч", a["summary"])

    def test_past_duty_accrued_immediately_and_kinds_split(self):
        eid = self.emp()
        past = (date.today() - timedelta(days=1)).isoformat()
        self.c.post("/api/duties", json={"date": past, "employee_id": eid, "kind": "official"})
        eid2 = self.emp("Петров Пётр", "Техник")
        self.c.post("/api/duties", json={"date": past, "employee_id": eid2, "kind": "unofficial", "hours": 8,
                                         "role": "shift"})
        off = self.hours("official", past[:7])
        unoff = self.hours("unofficial", past[:7])
        self.assertEqual([(r["employee_id"], r["hours"]) for r in off], [(eid, 24)])
        self.assertEqual([(r["employee_id"], r["hours"]) for r in unoff], [(eid2, 8)])

    def test_accrual_runs_on_startup(self):
        eid = self.emp()
        future = (date.today() + timedelta(days=1)).isoformat()
        self.c.post("/api/duties", json={"date": future, "employee_id": eid})
        db_path = self.app.config["DB_PATH"]
        with sqlite3.connect(db_path) as conn:  # «день наступил»: сдвигаем дежурство на вчера
            conn.execute("UPDATE duties SET duty_date=?", ((date.today() - timedelta(days=1)).isoformat(),))
        create_app(db_path)
        with sqlite3.connect(db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hours WHERE source='duty'").fetchone()[0], 1)

    def test_accrued_hours_follow_duty_edit_and_delete(self):
        eid = self.emp()
        past = (date.today() - timedelta(days=1)).isoformat()
        did = self.c.post("/api/duties", json={"date": past, "employee_id": eid}).get_json()["id"]
        hid = self.hours("official", past[:7])[0]["id"]
        # автоматическую запись нельзя править в таблице часов
        self.assertEqual(self.c.delete(f"/api/hours/official/{hid}").status_code, 409)
        self.assertEqual(self.c.put(f"/api/hours/official/{hid}", json={"date": past, "employee_id": eid,
                                                                        "hours": 1}).status_code, 409)
        r = self.c.put(f"/api/duties/{did}", json={"kind": "unofficial", "hours": 10, "role": "other"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.hours("official", past[:7]), [])
        self.assertEqual(self.hours("unofficial", past[:7])[0]["hours"], 10)
        self.c.delete(f"/api/duties/{did}")
        self.assertEqual(self.hours("unofficial", past[:7]), [])
        self.assertEqual(self.audit(entity="duty", q="Изменил")["total"], 1)

    # --- отгулы ---
    def test_dayoff_writes_off_from_chosen_table(self):
        eid = self.emp()
        day = "2026-09-10"
        self.c.post("/api/hours/official", json={"date": "2026-09-01", "employee_id": eid, "hours": 20})
        r = self.c.post("/api/dayoffs", json={"date": day, "employee_id": eid})
        self.assertEqual(r.status_code, 201, r.get_json())
        off = r.get_json()
        self.assertEqual((off["kind"], off["hours"]), ("official", 8))
        rows = self.hours("official", "2026-09")
        self.assertEqual(sorted(x["hours"] for x in rows), [-8, 20])
        bal = self.c.get("/api/balance").get_json()[0]
        self.assertEqual((bal["official"], bal["unofficial"]), (12, 0))
        self.assertEqual(self.c.post("/api/dayoffs", json={"date": day, "employee_id": eid}).status_code, 409)
        # смена типа часов переносит списание в другую таблицу
        self.c.put(f"/api/dayoffs/{off['id']}", json={"kind": "unofficial", "hours": 4})
        self.assertEqual([x["hours"] for x in self.hours("unofficial", "2026-09")], [-4])
        self.assertEqual([x["hours"] for x in self.hours("official", "2026-09")], [20])
        # отмена отгула возвращает часы
        self.c.delete(f"/api/dayoffs/{off['id']}")
        self.assertEqual(self.hours("unofficial", "2026-09"), [])
        self.assertEqual(self.c.get("/api/dayoffs?month=2026-09").get_json(), [])
        self.assertGreaterEqual(self.audit(entity="dayoff")["total"], 3)
        self.assertIn("−8 ч", self.audit(entity="hours_official", q="Списал")["items"][0]["summary"])

    def test_dayoff_validation(self):
        eid = self.emp()
        for bad in ({"kind": "x"}, {"hours": 0}, {"hours": 25}, {"date": "bad"}, {"employee_id": 999}):
            body = {"date": "2026-09-10", "employee_id": eid, **bad}
            self.assertIn(self.c.post("/api/dayoffs", json=body).status_code, (400, 404), bad)

    def test_migrates_old_database(self):
        db_path = os.path.join(self.tmp.name, "old.db")
        with sqlite3.connect(db_path) as conn:
            conn.executescript("""
                CREATE TABLE employees (id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL,
                    position TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
                CREATE TABLE duties (id INTEGER PRIMARY KEY AUTOINCREMENT, duty_date TEXT NOT NULL,
                    employee_id INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', UNIQUE (duty_date, employee_id));
                CREATE TABLE hours (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, work_date TEXT NOT NULL,
                    employee_id INTEGER NOT NULL, hours REAL NOT NULL, comment TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL, created_at TEXT NOT NULL);
                INSERT INTO employees (full_name, position, created_at) VALUES ('Старый', 'Инженер', '2026-01-01');
                INSERT INTO duties (duty_date, employee_id) VALUES ('2026-01-05', 1);
            """)
        c = create_app(db_path).test_client()
        d = c.get("/api/duties?month=2026-01").get_json()[0]
        self.assertEqual((d["kind"], d["hours"], d["role"]), ("official", 24, "duty"))
        # прошлые дежурства из старой базы задним числом не начисляются (часы могли внести вручную)
        self.assertEqual(c.get("/api/hours/official?month=2026-01").get_json(), [])

    def test_index_served(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Переработка".encode(), r.data)


if __name__ == "__main__":
    unittest.main()


class WindowsAuthTest(unittest.TestCase):
    def make(self, mode="auto", allow_manual=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        if allow_manual is not None:
            os.environ["PERER_ALLOW_MANUAL"] = allow_manual
            self.addCleanup(os.environ.pop, "PERER_ALLOW_MANUAL", None)
        app = create_app(os.path.join(self.tmp.name, "t.db"), auth_mode=mode,
                         authenticator=FakeAuthenticator())
        return app.test_client()

    def neg(self, c, token, port="5000"):
        import base64
        return c.get("/api/login/windows", environ_overrides={
            "REMOTE_ADDR": "10.0.0.7", "REMOTE_PORT": port},
            headers={"Authorization": "NTLM " + base64.b64encode(token).decode()})

    def test_handshake_sets_session(self):
        c = self.make()
        r = c.get("/api/login/windows", environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.getlist("WWW-Authenticate"), ["NTLM"])
        r = self.neg(c, b"type1")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers["WWW-Authenticate"], "NTLM Y2hhbGxlbmdl")
        r = self.neg(c, b"type3")
        self.assertEqual(r.status_code, 200, r.get_json())
        me = c.get("/api/me", environ_overrides={"REMOTE_ADDR": "10.0.0.7"}).get_json()
        self.assertEqual((me["login"], me["source"]), ("CORP\\ivanov", "windows"))
        c.post("/api/employees", json={"full_name": "Петров П.П.", "position": "Инженер"},
               environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
        audit = c.get("/api/audit").get_json()["items"][0]
        self.assertEqual(audit["actor"], "CORP\\ivanov")

    def test_handshake_other_connection_fails(self):
        c = self.make()
        self.neg(c, b"type1", port="5000")
        self.assertEqual(self.neg(c, b"type3", port="5001").status_code, 403)

    def test_logout(self):
        c = self.make()
        self.neg(c, b"type1")
        self.neg(c, b"type3")
        c.post("/api/logout", environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
        me = c.get("/api/me", environ_overrides={"REMOTE_ADDR": "10.0.0.7"}).get_json()
        self.assertEqual(me["source"], "unknown")
        self.assertTrue(me["windows_auth"])

    def test_windows_mode_blocks_manual_and_localhost_os_user(self):
        c = self.make("windows")
        local = {"REMOTE_ADDR": "127.0.0.1"}
        me = c.get("/api/me", environ_overrides=local).get_json()
        self.assertEqual(me["source"], "unknown")
        self.assertFalse(me["allow_manual"])
        self.assertEqual(c.post("/api/me", json={"name": "x"}).status_code, 403)
        r = c.post("/api/employees", json={"full_name": "А", "position": "Б"}, environ_overrides=local)
        self.assertEqual(r.status_code, 401)

    def test_off_mode_has_no_windows_login(self):
        c = self.make("off")
        self.assertFalse(c.get("/api/me").get_json()["windows_auth"])
        self.assertEqual(c.get("/api/login/windows").status_code, 404)

    def test_initial_token_detection(self):
        self.assertTrue(winauth.is_initial_token(b"\x60\x82rest"))
        self.assertTrue(winauth.is_initial_token(b"NTLMSSP\x00\x01\x00\x00\x00..."))
        self.assertFalse(winauth.is_initial_token(b"NTLMSSP\x00\x03\x00\x00\x00..."))
        self.assertFalse(winauth.is_initial_token(b"\xa1\x81"))
