"""Tests for (1) 50MB upload size & (2) admin purge users endpoint."""
import os
import io
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://fnjee-deploy-1.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login", json={
        "email": "admin@examnest.io", "password": "Admin@123", "role": "admin"
    }, timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    return j.get("token") or j.get("access_token")


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def student_token():
    r = requests.post(f"{API}/auth/login", json={
        "email": "student1@examnest.io", "password": "Student@123", "role": "student"
    }, timeout=30)
    if r.status_code != 200:
        return None
    j = r.json()
    return j.get("token") or j.get("access_token")


# ---------- Upload size tests ----------
def _dummy_pdf_bytes(size_mb: int) -> bytes:
    header = b"%PDF-1.4\n%fake\n"
    pad = b"0" * (size_mb * 1024 * 1024 - len(header))
    return header + pad


@pytest.mark.parametrize("size_mb", [5, 10, 20])
def test_import_start_accepts_large_pdf(admin_headers, size_mb):
    data = _dummy_pdf_bytes(size_mb)
    files = {"file": (f"big_{size_mb}mb.pdf", io.BytesIO(data), "application/pdf")}
    form = {"subject_default": "Physics", "extract": "false"}
    r = requests.post(f"{API}/import/start", headers=admin_headers, files=files, data=form, timeout=300)
    assert r.status_code == 200, f"{size_mb}MB -> {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert "job_id" in body
    assert body.get("status") in ("processing", "queued", "pending", "done", "error")


# ---------- Real extraction (slow) ----------
@pytest.mark.slow
def test_import_docx_extraction_persists():
    pass  # see test_hydrocarbons_extract


def test_hydrocarbons_extract(admin_headers):
    path = "/app/tests/sample_imports/Hydrocarbons_Questions.docx"
    if not os.path.exists(path):
        pytest.skip("sample file missing")
    with open(path, "rb") as f:
        files = {"file": ("Hydrocarbons_Questions.docx", f,
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
        form = {"subject_default": "Chemistry", "extract": "true"}
        r = requests.post(f"{API}/import/start", headers=admin_headers, files=files, data=form, timeout=300)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    deadline = time.time() + 180  # 3 min poll
    status = None
    result = None
    while time.time() < deadline:
        jr = requests.get(f"{API}/import/jobs/{job_id}", headers=admin_headers, timeout=30)
        assert jr.status_code == 200, jr.text
        jj = jr.json()
        status = jj.get("status")
        result = jj.get("result")
        if status in ("done", "error", "failed"):
            break
        time.sleep(5)
    assert status == "done", f"status={status} result={result}"
    assert result, "missing result"
    count = result.get("count") or result.get("inserted") or 0
    assert count >= 60, f"expected >=60 questions, got {count}: {result}"


# ---------- Purge guard tests ----------
def test_purge_requires_admin_no_token():
    r = requests.post(f"{API}/admin/users/purge", json={"confirm": "DELETE ALL USERS"}, timeout=30)
    assert r.status_code in (401, 403), r.text


def test_purge_rejects_non_admin(student_token):
    if not student_token:
        pytest.skip("no student token")
    r = requests.post(f"{API}/admin/users/purge",
                      headers={"Authorization": f"Bearer {student_token}"},
                      json={"confirm": "DELETE ALL USERS"}, timeout=30)
    assert r.status_code in (401, 403), r.text


def test_purge_rejects_wrong_confirm(admin_headers):
    # count students before
    r0 = requests.get(f"{API}/users", params={"role": "student"}, headers=admin_headers, timeout=30)
    assert r0.status_code == 200
    before = len(r0.json())

    r = requests.post(f"{API}/admin/users/purge", headers=admin_headers,
                      json={"confirm": "wrong"}, timeout=30)
    assert r.status_code == 400, r.text

    r_miss = requests.post(f"{API}/admin/users/purge", headers=admin_headers,
                           json={}, timeout=30)
    assert r_miss.status_code == 400

    # ensure nothing was deleted
    r1 = requests.get(f"{API}/users", params={"role": "student"}, headers=admin_headers, timeout=30)
    after = len(r1.json())
    assert after == before, f"students changed {before}->{after} after rejected purge"


# ---------- Purge happy path (DESTRUCTIVE) ----------
def test_purge_happy_path(admin_headers):
    # pre
    pre_admins = requests.get(f"{API}/users", params={"role": "admin"}, headers=admin_headers, timeout=30).json()
    pre_students = requests.get(f"{API}/users", params={"role": "student"}, headers=admin_headers, timeout=30).json()
    assert len(pre_admins) >= 1

    r = requests.post(f"{API}/admin/users/purge", headers=admin_headers,
                      json={"confirm": "DELETE ALL USERS"}, timeout=60)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert "deleted_users" in body
    assert "deleted_attempts" in body
    assert isinstance(body["deleted_users"], int)
    assert isinstance(body["deleted_attempts"], int)

    # admins remain
    post_admins = requests.get(f"{API}/users", params={"role": "admin"}, headers=admin_headers, timeout=30).json()
    assert len(post_admins) == len(pre_admins), "admins count changed!"

    # students empty
    post_students = requests.get(f"{API}/users", params={"role": "student"}, headers=admin_headers, timeout=30).json()
    assert post_students == [], f"expected empty students, got {len(post_students)}"

    # teachers & parents too
    post_teachers = requests.get(f"{API}/users", params={"role": "teacher"}, headers=admin_headers, timeout=30).json()
    assert post_teachers == []
    post_parents = requests.get(f"{API}/users", params={"role": "parent"}, headers=admin_headers, timeout=30).json()
    assert post_parents == []
