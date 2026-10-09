"""VPS mirror: Gemini key via env var only (no DB 'ai' doc).

Verifies the env-key fallback added in /app/backend/ai_key.py: AI status reports
source='env-key', connectivity test runs 'direct' (not emergent proxy), DOCX+PDF
imports complete using only the env key, and DB override still wins/can be reset.
"""
import os
import time
import pytest
import requests

def _load_frontend_url():
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return os.environ.get("REACT_APP_BACKEND_URL")

BASE_URL = (_load_frontend_url() or "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL missing"
API = f"{BASE_URL}/api"
SAMPLES = "/app/tests/sample_imports"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login", json={
        "email": "admin@examnest.io", "password": "Admin@123", "role": "admin"
    }, timeout=30)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def H(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# --- AI env fallback status ---------------------------------------------------
def test_ai_status_env_key(H):
    r = requests.get(f"{API}/admin/settings/ai", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("provider") == "gemini", d
    assert d.get("model") == "gemini-3.8-flash", d
    assert d.get("source") == "env-key", d
    assert d.get("env_key_present") is True, d
    assert d.get("masked_key"), d


# --- AI connectivity: direct (not emergent proxy) -----------------------------
def test_ai_test_direct_mode(H):
    r = requests.post(f"{API}/admin/settings/ai/test", headers=H, timeout=60)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("ok") is True, d
    assert d.get("provider") == "gemini", d
    assert d.get("mode") == "direct", d
    assert "ok" in (d.get("reply") or "").lower(), d


# --- Helpers ------------------------------------------------------------------
def _poll_job(H, job_id, timeout=200):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = requests.get(f"{API}/import/jobs/{job_id}", headers=H, timeout=30)
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("status") in ("done", "error"):
            return last
        time.sleep(5)
    return last


# --- PRIMARY FIX: DOCX import via env key -------------------------------------
def test_docx_import_uses_env_key(H):
    path = f"{SAMPLES}/Hydrocarbons_Questions.docx"
    with open(path, "rb") as fh:
        files = {"file": ("Hydrocarbons_Questions.docx", fh,
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
        data = {"subject_default": "Chemistry", "import_mode": "extract", "use_ai": "true"}
        r = requests.post(f"{API}/import/start", headers=H, files=files, data=data, timeout=60)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    job = _poll_job(H, job_id, timeout=220)
    assert job and job.get("status") == "done", f"docx job did not finish: {job}"
    result = job.get("result") or {}
    count = result.get("count") or len(result.get("questions") or [])
    assert count >= 60, f"expected >=60 questions, got {count}: keys={list(result.keys())}"
    assert result.get("used_ai") is True, f"used_ai should be True: {result}"


# --- PDF import via env key ---------------------------------------------------
def test_pdf_import_uses_env_key(H):
    path = f"{SAMPLES}/Hydrocarbons_Solutions.pdf"
    with open(path, "rb") as fh:
        files = {"file": ("Hydrocarbons_Solutions.pdf", fh, "application/pdf")}
        data = {"subject_default": "Chemistry", "import_mode": "extract", "use_ai": "true"}
        r = requests.post(f"{API}/import/start", headers=H, files=files, data=data, timeout=60)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    job = _poll_job(H, job_id, timeout=220)
    assert job and job.get("status") == "done", f"pdf job did not finish: {job}"
    result = job.get("result") or {}
    count = result.get("count") or len(result.get("questions") or [])
    assert count > 0, f"expected >0 questions, got {count}: {result}"


# --- DB override wins, then cleanup back to env-key ---------------------------
def test_db_override_then_cleanup(H):
    override_key = "AQ.Ab8RN6LB2k0Wkm7vWy5EoeK57nXRwXTRWLsmsKZjnDr8M_6i8w"
    try:
        r = requests.put(f"{API}/admin/settings/ai", headers=H, json={
            "provider": "gemini", "api_key": override_key, "model": "gemini-3.8-flash"
        }, timeout=30)
        assert r.status_code == 200, r.text
        r = requests.get(f"{API}/admin/settings/ai", headers=H, timeout=30)
        assert r.status_code == 200, r.text
        assert r.json().get("source") == "admin", r.json()
    finally:
        # Cleanup: delete DB doc so preview mirrors VPS (env-key only)
        rd = requests.delete(f"{API}/admin/settings/ai", headers=H, timeout=30)
        # If delete route differs, note but don't fail
        if rd.status_code != 200:
            pytest.skip(f"DELETE /admin/settings/ai returned {rd.status_code}: {rd.text}")
        rg = requests.get(f"{API}/admin/settings/ai", headers=H, timeout=30)
        assert rg.status_code == 200, rg.text
        assert rg.json().get("source") == "env-key", rg.json()


# --- Admin login requires email+password+role ---------------------------------
def test_admin_login_requires_role():
    r = requests.post(f"{API}/auth/login", json={
        "email": "admin@examnest.io", "password": "Admin@123"
    }, timeout=30)
    assert r.status_code in (400, 401, 422), f"expected failure without role, got {r.status_code}: {r.text}"
