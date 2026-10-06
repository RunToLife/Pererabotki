import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.server import create_app  # noqa: E402


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

    def test_index_served(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Переработка".encode(), r.data)


if __name__ == "__main__":
    unittest.main()
