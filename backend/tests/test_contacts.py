"""Saved contacts: validation, CRUD, auth, audit."""

from fastapi.testclient import TestClient

from app import db
from tests.conftest import LOCAL, login

ADDR = "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"
ADDR2 = "SeLxbTthMTY1iMFbbBU4Du52BMR9BNSfu6"


def test_contacts_require_session(client: TestClient):
    assert client.get("/api/contacts", headers=LOCAL).status_code == 401


def test_contacts_empty_then_add_list(client: TestClient):
    out = login(client, headers=LOCAL)
    assert client.get("/api/contacts", headers=out["headers"]).json()["contacts"] == []
    r = client.post("/api/contacts", json={"label": "  Alice  ", "address": ADDR},
                    headers=out["headers"])
    assert r.status_code == 200
    rows = client.get("/api/contacts", headers=out["headers"]).json()["contacts"]
    assert [(c["label"], c["address"]) for c in rows] == [("Alice", ADDR)]


def test_contact_add_requires_csrf(client: TestClient):
    out = login(client, headers=LOCAL)
    hdr = {k: v for k, v in out["headers"].items() if k != "x-csrf-token"}
    r = client.post("/api/contacts", json={"label": "A", "address": ADDR}, headers=hdr)
    assert r.status_code == 403


def test_contact_rejects_bad_address_and_empty_label(client: TestClient):
    out = login(client, headers=LOCAL)
    # Right shape, wrong checksum.
    bad = ADDR[:-1] + ("2" if ADDR[-1] != "2" else "3")
    r = client.post("/api/contacts", json={"label": "A", "address": bad},
                    headers=out["headers"])
    assert r.status_code == 422
    r = client.post("/api/contacts", json={"label": "  ", "address": ADDR},
                    headers=out["headers"])
    assert r.status_code == 422


def test_contact_duplicate_address_conflicts(client: TestClient):
    out = login(client, headers=LOCAL)
    client.post("/api/contacts", json={"label": "A", "address": ADDR}, headers=out["headers"])
    r = client.post("/api/contacts", json={"label": "B", "address": ADDR},
                    headers=out["headers"])
    assert r.status_code == 409


def test_contact_rename_delete_and_audit(client: TestClient, tmp_path):
    out = login(client, headers=LOCAL)
    cid = client.post("/api/contacts", json={"label": "A", "address": ADDR2},
                      headers=out["headers"]).json()["id"]
    assert client.put(f"/api/contacts/{cid}", json={"label": "Bob"},
                      headers=out["headers"]).status_code == 200
    rows = client.get("/api/contacts", headers=out["headers"]).json()["contacts"]
    assert rows[0]["label"] == "Bob"
    assert client.delete(f"/api/contacts/{cid}", headers=out["headers"]).status_code == 200
    assert client.delete(f"/api/contacts/{cid}", headers=out["headers"]).status_code == 404
    assert client.put("/api/contacts/999", json={"label": "x"},
                      headers=out["headers"]).status_code == 404
    dbp = client.app.state.app_state.settings.db_path
    actions = [a["action"] for a in db.audit_list(dbp, 50)]
    for a in ("contact_add", "contact_rename", "contact_delete"):
        assert a in actions


def test_contact_limit(client: TestClient, monkeypatch):
    out = login(client, headers=LOCAL)
    monkeypatch.setattr(db, "MAX_CONTACTS", 1)
    assert client.post("/api/contacts", json={"label": "A", "address": ADDR},
                       headers=out["headers"]).status_code == 200
    assert client.post("/api/contacts", json={"label": "B", "address": ADDR2},
                       headers=out["headers"]).status_code == 409
