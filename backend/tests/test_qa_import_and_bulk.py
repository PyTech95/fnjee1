"""Tests for Q+A PDF AI import and Question Bank bulk-delete/bulk-update."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://fnjee-deploy-1.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

Q_PDF = "/app/tests/sample_imports/Hydrocarbons_Q.pdf"
A_PDF = "/app/tests/sample_imports/Hydrocarbons_A.pdf"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login", json={
        "email": "admin@examnest.io", "password": "Admin@123", "role": "admin"
    }, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def h(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# ---------- Bulk delete ----------
def _create_question(h, text):
    r = requests.post(f"{API}/questions", headers=h, json={
        "text": text, "subject": "Physics", "type": "mcq_single",
        "options": ["a", "b", "c", "d"], "correct": ["A"], "marks": 4, "status": "approved"
    }, timeout=30)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_bulk_delete(h):
    ids = [_create_question(h, f"BULKTEST Q{i}") for i in range(3)]
    r = requests.post(f"{API}/questions/bulk-delete", headers=h, json={"ids": ids}, timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("deleted") == 3
    # Confirm gone
    g = requests.get(f"{API}/questions", headers=h, params={"limit": 500}, timeout=30).json()
    items = g.get("items") if isinstance(g, dict) else g
    existing = {q["id"] for q in items}
    for i in ids:
        assert i not in existing


def test_bulk_delete_empty_ids(h):
    r = requests.post(f"{API}/questions/bulk-delete", headers=h, json={"ids": []}, timeout=30)
    assert r.status_code == 400


def test_bulk_delete_requires_admin():
    r = requests.post(f"{API}/questions/bulk-delete", json={"ids": ["x"]}, timeout=30)
    assert r.status_code in (401, 403)


# ---------- Bulk update ----------
def test_bulk_update(h):
    ids = [_create_question(h, f"BULKTEST U{i}") for i in range(2)]
    r = requests.post(f"{API}/questions/bulk-update", headers=h, json={
        "ids": ids, "patch": {"difficulty": "hard", "chapter": "BULKCHAP"},
        "add_tags": ["btag"]
    }, timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("modified", 0) >= 1
    # verify via list
    g = requests.get(f"{API}/questions", headers=h, params={"limit": 500}, timeout=30).json()
    items = g.get("items") if isinstance(g, dict) else g
    by_id = {q["id"]: q for q in items}
    for qid in ids:
        q = by_id.get(qid)
        assert q is not None, f"missing {qid}"
        assert q.get("difficulty") == "hard"
        assert q.get("chapter") == "BULKCHAP"
        assert "btag" in (q.get("tags") or [])
    # cleanup
    requests.post(f"{API}/questions/bulk-delete", headers=h, json={"ids": ids}, timeout=30)


# ---------- Q+A PDF AI import ----------
@pytest.mark.timeout(300)
def test_qa_pdf_import(h):
    assert os.path.exists(Q_PDF) and os.path.exists(A_PDF)
    with open(Q_PDF, "rb") as qf, open(A_PDF, "rb") as af:
        files = {
            "file": ("Hydrocarbons_Q.pdf", qf, "application/pdf"),
            "answer_file": ("Hydrocarbons_A.pdf", af, "application/pdf"),
        }
        data = {
            "subject_default": "Chemistry",
            "prefer_ai": "true",
            "import_mode": "extract",
            "file_type_hint": "pdf",
        }
        r = requests.post(f"{API}/import/start", headers=h, files=files, data=data, timeout=60)
    assert r.status_code == 200, r.text
    job_id = r.json().get("job_id")
    assert job_id

    deadline = time.time() + 240
    job = None
    while time.time() < deadline:
        g = requests.get(f"{API}/import/jobs/{job_id}", headers=h, timeout=30)
        assert g.status_code == 200, g.text
        job = g.json()
        if job.get("status") in ("done", "error"):
            break
        time.sleep(5)
    assert job and job.get("status") == "done", f"job final state: {job}"
    result = job.get("result") or {}
    count = result.get("count", 0)
    print(f"Imported count={count} used_ai={result.get('used_ai')} answer_key_applied={result.get('answer_key_applied')}")
    assert count >= 70, f"expected >=70 questions, got {count}"
    assert result.get("used_ai") is True
    assert (result.get("answer_key_applied") or 0) >= 70

    qs = result.get("questions") or []
    # First several should be mcq_single with 4 options and correct set
    first = qs[:4]
    for q in first:
        assert q.get("type") == "mcq_single", q
        assert len(q.get("options") or []) == 4, q
        assert q.get("correct"), q
    # Expected specific answers (per request)
    expected = {0: "C", 1: "A", 2: "D", 3: "B"}
    for idx, letter in expected.items():
        c = qs[idx].get("correct")
        assert c and str(c[0]).upper() == letter, f"Q{idx+1} expected {letter}, got {c}"
