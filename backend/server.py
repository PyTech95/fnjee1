"""ExamNest backend - FastAPI + MongoDB + JWT auth + question bank + test engine."""
from fastapi import FastAPI, APIRouter, HTTPException, Depends, UploadFile, File, Form, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from pathlib import Path
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, EmailStr
from datetime import datetime, timezone, timedelta
import os, uuid, logging, jwt, bcrypt, random, string, re, traceback, asyncio

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from parsers import (parse_excel, parse_docx, parse_pdf, parse_pagemaker,
                     download_google_drive, extract_text_any, image_to_pdf, IMAGE_EXTS)
from ai_parser import ai_extract_questions, ai_predict_difficulty, ai_generate_quiz
from visual_pdf import parse_visual_pdf
from regex_extractor import regex_extract_questions
from ai_key import resolve as resolve_ai, resolve_full as ai_full, load_from_db as load_ai_cfg, status as ai_status, PROVIDERS as AI_PROVIDERS
from ai_chat import ai_complete, OWN_DEFAULT_MODEL

MONGO_URL = os.environ['MONGO_URL']
DB_NAME = os.environ['DB_NAME']
JWT_SECRET = os.environ['JWT_SECRET']
CORS_ORIGINS = [o.strip() for o in os.environ['CORS_ORIGINS'].split(',') if o.strip()]
if os.environ.get('ENV') == 'production' and (len(JWT_SECRET) < 32 or '*' in CORS_ORIGINS):
    raise RuntimeError("Production requires JWT_SECRET >= 32 chars and explicit CORS_ORIGINS (no '*')")
JWT_ALGO = 'HS256'
JWT_EXP_DAYS = 30

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

app = FastAPI(title="ExamNest API")
api = APIRouter(prefix="/api")
security = HTTPBearer(auto_error=False)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("examnest")


def now_iso() -> str: return datetime.now(timezone.utc).isoformat()
def new_id() -> str: return str(uuid.uuid4())
def hash_pw(pw: str) -> str: return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
def check_pw(pw: str, hashed: str) -> bool:
    try: return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception: return False
def make_token(user_id: str, role: str) -> str:
    payload = {"sub": user_id, "role": role, "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXP_DAYS)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)
def gen_referral(prefix: str = "EXN") -> str:
    return prefix + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
def clean(d):
    if d and "_id" in d: d.pop("_id")
    return d


from collections import defaultdict
import time as _time
import math as _math
_rate_buckets: dict = defaultdict(list)

async def rate_limit(key: str, limit: int, window_seconds: int):
    """Distributed fixed-window rate limiter backed by MongoDB.

    Uses a shared collection so limits hold across multiple uvicorn/gunicorn
    workers (unlike an in-memory dict). A TTL index on `expire_at` auto-purges
    stale windows. Designed to be swapped for Redis (INCR + EXPIRE) later with
    no call-site changes.
    """
    now = _time.time()
    window_start = int(now // window_seconds) * window_seconds
    doc_id = f"{key}:{window_start}"
    try:
        res = await db.rate_limits.find_one_and_update(
            {"_id": doc_id},
            {"$inc": {"count": 1},
             "$setOnInsert": {"expire_at": datetime.fromtimestamp(window_start + window_seconds * 2, tz=timezone.utc)}},
            upsert=True,
            return_document=True,
        )
        count = (res or {}).get("count", 1)
    except Exception:
        # Fail-open on limiter storage errors: never block legitimate traffic
        # because the limiter backend hiccuped. Fall back to in-memory.
        bucket = _rate_buckets[key]
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= limit:
            raise HTTPException(429, "Too many requests. Please slow down and try again shortly.")
        bucket.append(now)
        return
    if count > limit:
        retry = int(window_start + window_seconds - now) + 1
        raise HTTPException(429, f"Too many requests. Please slow down and retry in ~{retry}s.")

def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


async def get_current_user(cred: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> dict:
    if not cred: raise HTTPException(401, "Missing token")
    try: payload = jwt.decode(cred.credentials, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.PyJWTError: raise HTTPException(401, "Invalid token")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password": 0})
    if not user: raise HTTPException(401, "User not found")
    return user

def require_role(*roles):
    async def dep(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles: raise HTTPException(403, f"Role {user['role']} not permitted")
        return user
    return dep


class SignupIn(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: Literal['student', 'parent', 'admin', 'teacher'] = 'student'
    referral_code: Optional[str] = None
    child_email: Optional[str] = None
    exam_target: Optional[str] = None

class LoginIn(BaseModel):
    email: EmailStr
    password: str
    role: Literal['student', 'parent', 'admin', 'teacher']

class QuestionIn(BaseModel):
    type: str = 'mcq_single'
    subject: str
    chapter: Optional[str] = ''
    topic: Optional[str] = ''
    difficulty: Literal['easy', 'medium', 'hard'] = 'medium'
    marks: float = 4
    negative_marks: float = 1
    text: str
    options: List[str] = []
    correct: List[str] = []
    explanation: str = ''
    hint: str = ''
    language: str = 'English'
    image_url: Optional[str] = None
    image_alt: str = 'Question diagram'
    explanation_image_url: Optional[str] = None
    content_origin: Optional[str] = None
    source_number: Optional[int] = None
    source: Optional[str] = None
    status: Literal['draft', 'review', 'approved', 'archived'] = 'approved'
    # Extended metadata (spec: exam / class / year / paper series / tags)
    exam: Optional[str] = ''
    student_class: Optional[str] = ''
    year: Optional[str] = ''
    series: Optional[str] = ''
    tags: List[str] = []
    # CBT extensions (all optional, non-breaking)
    text_hi: Optional[str] = None
    options_hi: List[str] = []
    section: Optional[str] = None
    numerical_validation: Optional[dict] = None
    partial_marks_config: Optional[dict] = None

class TeacherPermIn(BaseModel):
    teacher_id: str
    exams: List[str] = []
    subjects: List[str] = []
    classes: List[str] = []
    can_print: bool = True
    can_view_results: bool = True

class TestIn(BaseModel):
    title: str
    exam_type: str = 'full_mock'
    description: str = ''
    subjects: List[str] = []
    duration_minutes: int = 60
    total_marks: Optional[float] = None
    negative_marking: bool = True
    shuffle_questions: bool = True
    shuffle_options: bool = False
    show_solutions_after: bool = True
    scheduled_at: Optional[str] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    assigned_to: List[str] = []
    sections: List[dict] = []
    question_ids: List[str] = []
    proctor_cutoff: int = 0
    proctor_action: str = 'flag'
    instructions_en: Optional[str] = None
    instructions_hi: Optional[str] = None
    published: bool = True

class AttemptStartIn(BaseModel):
    test_id: str
    use_retake_pass: bool = False

class AttemptAnswerIn(BaseModel):
    question_id: str
    answer: List[str] = []
    marked_review: bool = False
    confidence: Optional[Literal['sure', 'guess']] = None
    image_answer: Optional[str] = None  # base64 data URL for handwritten answers (subjective)
    time_taken: Optional[int] = None    # seconds spent on this question (client-reported)

class AttemptSubmitIn(BaseModel):
    attempt_id: str
    answers: List[AttemptAnswerIn] = []

class AssignTestIn(BaseModel):
    test_id: str
    student_ids: List[str]

class ParentAssignIn(BaseModel):
    title: str
    subjects: List[str]
    difficulty: Literal['easy', 'medium', 'hard'] = 'medium'
    num_questions: int = 10
    duration_minutes: int = 30
    child_id: str

class AnnouncementIn(BaseModel):
    title: str
    body: str
    audience: Literal['all', 'students', 'parents'] = 'all'


@api.post("/auth/signup")
async def signup(inp: SignupIn):
    if await db.users.find_one({"email": inp.email.lower()}):
        raise HTTPException(400, "Email already registered")
    uid = new_id()
    doc = {
        "id": uid, "name": inp.name, "email": inp.email.lower(),
        "password": hash_pw(inp.password), "role": inp.role, "created_at": now_iso(),
        "referral_code": gen_referral(), "referred_by": None,
        "reward_coins": 0, "streak_days": 0, "last_active": now_iso(),
        "exam_target": inp.exam_target or ("JEE" if inp.role == 'student' else None),
        "child_ids": [], "parent_ids": [],
        "freeze_available": True, "last_freeze_used_at": None,
        "email_results_enabled": True, "reminder_hour": 19,
        "streak_freezes": 0, "hint_tokens": 0, "retake_passes": 0,
        "earned_badges": [], "alert_drop_threshold": 15, "alert_mode": "instant", "pinned_badge": None,
        "avatar": f"https://api.dicebear.com/7.x/initials/svg?seed={inp.name}",
    }
    if inp.referral_code:
        ref = await db.users.find_one({"referral_code": inp.referral_code})
        if ref:
            doc["referred_by"] = ref["id"]
            await db.users.update_one({"id": ref["id"]}, {"$inc": {"reward_coins": 100}})
            await db.referrals.insert_one({"id": new_id(), "referrer_id": ref["id"], "referee_id": uid,
                                          "code": inp.referral_code, "created_at": now_iso(), "coins_awarded": 100})
    if inp.role == 'parent' and inp.child_email:
        child = await db.users.find_one({"email": inp.child_email.lower(), "role": "student"})
        if child:
            doc["child_ids"] = [child["id"]]
            await db.users.update_one({"id": child["id"]}, {"$addToSet": {"parent_ids": uid}})
    await db.users.insert_one(doc)
    token = make_token(uid, inp.role)
    doc.pop("password", None); doc.pop("_id", None)
    return {"token": token, "user": doc}


@api.post("/auth/login")
async def login(inp: LoginIn, request: Request):
    await rate_limit(f"login:{client_ip(request)}", limit=10, window_seconds=60)
    u = await db.users.find_one({"email": inp.email.lower(), "role": inp.role})
    if not u or not check_pw(inp.password, u["password"]):
        raise HTTPException(401, "Invalid credentials or wrong role")
    await db.users.update_one({"id": u["id"]}, {"$set": {"last_active": now_iso()}})
    token = make_token(u["id"], u["role"])
    u.pop("password", None); u.pop("_id", None)
    return {"token": token, "user": u}


@api.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return user


class SettingsIn(BaseModel):
    email_results_enabled: Optional[bool] = None
    reminder_hour: Optional[int] = None
    alert_drop_threshold: Optional[int] = None
    alert_mode: Optional[str] = None
    pinned_badge: Optional[str] = None


@api.put("/auth/me/settings")
async def update_my_settings(inp: SettingsIn, user: dict = Depends(get_current_user)):
    upd = {}
    if inp.email_results_enabled is not None:
        upd["email_results_enabled"] = inp.email_results_enabled
    if inp.reminder_hour is not None:
        upd["reminder_hour"] = max(0, min(23, int(inp.reminder_hour)))
    if inp.alert_drop_threshold is not None:
        upd["alert_drop_threshold"] = max(5, min(50, int(inp.alert_drop_threshold)))
    if inp.alert_mode is not None:
        upd["alert_mode"] = inp.alert_mode if inp.alert_mode in ("instant", "weekly") else "instant"
    if inp.pinned_badge is not None:
        upd["pinned_badge"] = inp.pinned_badge
    if upd:
        await db.users.update_one({"id": user["id"]}, {"$set": upd})
    return await db.users.find_one({"id": user["id"]}, {"_id": 0, "password": 0})


@api.get("/users")
async def list_users(role: Optional[str] = None, user: dict = Depends(require_role('admin'))):
    q = {"role": role} if role else {}
    docs = await db.users.find(q, {"_id": 0, "password": 0}).to_list(500)
    return docs

@api.get("/users/{uid}")
async def get_user(uid: str, user: dict = Depends(get_current_user)):
    u = await db.users.find_one({"id": uid}, {"_id": 0, "password": 0})
    if not u: raise HTTPException(404, "Not found")
    return u


@api.post("/admin/users/purge")
async def purge_all_users(payload: dict, user: dict = Depends(require_role('admin'))):
    """Danger zone: delete EVERY non-admin user (students, parents, teachers) and
    their attempts in one shot. Admin accounts are kept so you don't lock yourself out.
    Requires an explicit confirmation string to avoid accidents."""
    if (payload or {}).get("confirm") != "DELETE ALL USERS":
        raise HTTPException(400, "Confirmation phrase required")
    victims = await db.users.find({"role": {"$ne": "admin"}}, {"_id": 0, "id": 1}).to_list(1000000)
    ids = [v["id"] for v in victims]
    du = await db.users.delete_many({"role": {"$ne": "admin"}})
    da = await db.attempts.delete_many({"user_id": {"$in": ids}}) if ids else None
    log.warning(f"ADMIN PURGE by {user.get('email')}: removed {du.deleted_count} users")
    return {"ok": True, "deleted_users": du.deleted_count,
            "deleted_attempts": (da.deleted_count if da else 0)}


@api.get("/questions")
async def list_questions(
    subject: Optional[str] = None, chapter: Optional[str] = None,
    topic: Optional[str] = None, difficulty: Optional[str] = None,
    q_type: Optional[str] = None, status_f: Optional[str] = None,
    exam: Optional[str] = None, student_class: Optional[str] = None,
    year: Optional[str] = None, series: Optional[str] = None,
    search: Optional[str] = None, limit: int = 200,
    user: dict = Depends(require_role('admin', 'teacher')),
):
    q: dict = {}
    if subject: q["subject"] = subject
    if chapter: q["chapter"] = chapter
    if topic: q["topic"] = topic
    if difficulty: q["difficulty"] = difficulty
    if q_type: q["type"] = q_type
    if status_f: q["status"] = status_f
    if exam: q["exam"] = exam
    if student_class: q["student_class"] = student_class
    if year: q["year"] = year
    if series: q["series"] = series
    if search: q["text"] = {"$regex": search, "$options": "i"}
    q["personal"] = {"$ne": True}
    # Teachers only see questions within their granted subjects (empty = unrestricted).
    # Exam/class are metadata that legacy questions may lack, so they are not hard-filtered here.
    if user["role"] == "teacher":
        perms = user.get("teacher_perms") or {}
        if perms.get("subjects"):
            allowed = perms["subjects"]
            if subject and subject in allowed:
                q["subject"] = subject
            else:
                q["subject"] = {"$in": allowed}
    docs = await db.questions.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return docs

@api.post("/questions")
async def create_question(inp: QuestionIn, user: dict = Depends(require_role('admin'))):
    dup = await db.questions.find_one({"text": inp.text}, {"_id": 0, "id": 1})
    doc = inp.model_dump()
    doc.update({"id": new_id(), "created_at": now_iso(), "created_by": user["id"],
                "duplicate_of": dup["id"] if dup else None})
    await db.questions.insert_one(doc)
    return clean(doc)

@api.put("/questions/{qid}")
async def update_question(qid: str, inp: QuestionIn, user: dict = Depends(require_role('admin'))):
    upd = inp.model_dump(); upd["updated_at"] = now_iso()
    r = await db.questions.update_one({"id": qid}, {"$set": upd})
    if r.matched_count == 0: raise HTTPException(404, "Not found")
    return await db.questions.find_one({"id": qid}, {"_id": 0})

@api.patch("/questions/{qid}")
async def patch_question(qid: str, patch: dict, user: dict = Depends(require_role('admin'))):
    cur = await db.questions.find_one({"id": qid}, {"_id": 0})
    if not cur: raise HTTPException(404, "Not found")
    unknown = set(patch) - set(QuestionIn.model_fields)
    if unknown: raise HTTPException(400, f"Unknown fields: {', '.join(sorted(unknown))}")
    merged = {k: v for k, v in cur.items() if k in QuestionIn.model_fields} | patch
    try:
        valid = QuestionIn(**merged).model_dump()
    except Exception as e:
        raise HTTPException(422, str(e)[:400])
    upd = {k: valid[k] for k in patch}; upd["updated_at"] = now_iso()
    await db.questions.update_one({"id": qid}, {"$set": upd})
    return await db.questions.find_one({"id": qid}, {"_id": 0})

@api.delete("/questions/{qid}")
async def delete_question(qid: str, user: dict = Depends(require_role('admin'))):
    await db.questions.delete_one({"id": qid})
    return {"ok": True}


@api.post("/questions/bulk-update")
async def bulk_update_questions(payload: dict, user: dict = Depends(require_role('admin'))):
    """Apply the same field changes to many questions at once.
    Body: {ids: [...], patch: {chapter?, topic?, section?, difficulty?, status?, subject?,
           marks?, negative_marks?, exam?, student_class?, year?}, add_tags?: [..]}"""
    ids = payload.get("ids") or []
    patch = payload.get("patch") or {}
    add_tags = [t for t in (payload.get("add_tags") or []) if str(t).strip()]
    if not ids:
        raise HTTPException(400, "Select at least one question")
    allowed = {"chapter", "topic", "section", "difficulty", "status", "subject",
               "marks", "negative_marks", "exam", "student_class", "year", "type", "language"}
    upd = {k: v for k, v in patch.items() if k in allowed and v not in (None, "")}
    if not upd and not add_tags:
        raise HTTPException(400, "Nothing to change")
    modified = 0
    if upd:
        upd["updated_at"] = now_iso()
        r = await db.questions.update_many({"id": {"$in": ids}}, {"$set": upd})
        modified = r.modified_count
    if add_tags:
        r2 = await db.questions.update_many(
            {"id": {"$in": ids}}, {"$addToSet": {"tags": {"$each": add_tags}}})
        modified = max(modified, r2.modified_count)
    return {"ok": True, "modified": modified, "requested": len(ids)}


@api.post("/questions/bulk-delete")
async def bulk_delete_questions(payload: dict, user: dict = Depends(require_role('admin'))):
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(400, "Select at least one question")
    r = await db.questions.delete_many({"id": {"$in": ids}})
    return {"ok": True, "deleted": r.deleted_count}


async def _resolve_import_inputs(file, drive_url, file_type_hint, raw_text):
    """Normalise the three upload sources into (data, filename, ext, text)."""
    data: bytes = b""; filename = ""; text: str = ""; ext = ""
    if raw_text:
        text = raw_text.strip(); filename = "pasted-text.txt"; ext = "txt"
    elif drive_url:
        data, filename = await download_google_drive(drive_url)
        ext = (file_type_hint or filename.split(".")[-1] or "").lower()
    elif file:
        data = await file.read(); filename = file.filename or "upload"
        ext = (file_type_hint or filename.split(".")[-1] or "").lower()
    else:
        raise HTTPException(400, "Provide file, drive_url or raw_text")
    return data, filename, ext, text


async def _do_import_parse(*, data: bytes, filename: str, ext: str, text: str,
                           use_ai: bool, import_mode: str, subject_default: str,
                           type_default=None, difficulty_default=None, status_default=None,
                           chapter_default=None, topic_default=None, section_default=None,
                           exam_default=None, class_default=None, year_default=None,
                           tags_default=None, marks_default=None, negative_default=None,
                           answer_data: Optional[bytes] = None, answer_ext: str = "",
                           answer_text: Optional[str] = None, progress_cb=None,
                           prefer_ai: bool = False) -> dict:
    """Heavy lifting for an import: AI/regex extraction, categorisation, answer-key, dedupe.
    Pure bytes in → result dict out, so it can run synchronously OR in a background job."""
    async def _p(pct, msg):
        if progress_cb:
            try: await progress_cb(pct, msg)
            except Exception: pass
    await _p(5, "Reading file…")
    subject = subject_default or "Physics"
    # Never hard-fail on mode: default anything odd to a safe extract.
    if import_mode not in ("extract", "adapt"):
        import_mode = "extract"
    adapt_requested = (import_mode == "adapt")
    parsed: List[dict] = []; errors: List[str] = []; used_ai = False; used_regex = False
    document_kind = "questions"

    def _sig(q): return re.sub(r"\s+", " ", (q.get("text") or "").lower())[:120]

    def _merge(into, extra):
        seen = {_sig(q) for q in into}
        for q in extra:
            s = _sig(q)
            if s and s not in seen:
                seen.add(s); into.append(q)

    is_image = ext in IMAGE_EXTS
    is_spreadsheet = ext in ("xlsx", "xls")

    # ---------------- STEP 1: CODE-FIRST (deterministic, fast, free) ----------------
    # Skip the fast regex path when the admin asked for AI vision (prefer_ai) on a PDF —
    # scanned/complex question papers come out mangled via text regex.
    skip_code = prefer_ai and (ext == "pdf" or is_image)
    try:
        if is_spreadsheet:
            await _p(40, "Reading spreadsheet…")
            parsed, warns = parse_excel(data)
            errors.extend(warns); used_regex = bool(parsed)
        elif not is_image and not skip_code:
            if not text:
                await _p(15, "Extracting text…")
                text = extract_text_any(data, ext)
            if text and len(text) >= 30:
                await _p(22, "Scanning for questions…")
                parsed, warns = regex_extract_questions(text, subject)
                errors.extend(warns); used_regex = bool(parsed)
    except Exception as e:
        log.warning(f"code parse step failed softly: {e}")

    # ---------------- STEP 2: AI FALLBACK (only when code came up short) ----------------
    # The rule the user asked for: code first; if code can't do the job, AI converts
    # the file into questions. Images/scanned PDFs always need AI vision.
    need_ai = use_ai and (is_image or adapt_requested or prefer_ai or len(parsed) < 3)
    if need_ai:
        try:
            if ext == "pdf":
                await _p(30, "Code parse was thin — AI is reading the PDF…")
                ai_parsed, ai_errs, document_kind = await parse_visual_pdf(data, subject, import_mode, progress_cb=progress_cb)
                if import_mode == "extract" and document_kind == "solutions" and not ai_parsed:
                    await _p(55, "Solutions detected — generating practice questions…")
                    ai_parsed, adapt_warns, document_kind = await parse_visual_pdf(data, subject, "adapt", progress_cb=progress_cb)
                    ai_errs = ["Auto-detected a solutions-only document — generated adapted practice questions (you can review/edit each below)."] + adapt_warns
                used_ai = True
                _merge(parsed, [q for q in ai_parsed if isinstance(q, dict)])
                errors.extend(ai_errs)
            elif is_image:
                await _p(30, "AI is reading the image…")
                try:
                    pdf_bytes = image_to_pdf(data)
                except Exception as e:
                    pdf_bytes = None
                    errors.append(f"Could not read this image: {e}")
                if pdf_bytes:
                    mode = "adapt" if adapt_requested else "extract"
                    ai_parsed, ai_errs, document_kind = await parse_visual_pdf(pdf_bytes, subject, mode, progress_cb=progress_cb)
                    used_ai = True
                    _merge(parsed, [q for q in ai_parsed if isinstance(q, dict)])
                    errors.extend(ai_errs)
            elif text and len(text.strip()) >= 20:
                await _p(30, "AI is converting the file into questions…")
                ai_parsed, ai_errs = await ai_extract_questions(text, subject, progress_cb=progress_cb)
                used_ai = True
                _merge(parsed, [q for q in ai_parsed if isinstance(q, dict)])
                errors.extend(ai_errs)
            elif not is_spreadsheet and not parsed:
                errors.append("We couldn't read any text from this file. Please upload a PDF, Word (.docx), PowerPoint (.pptx), image, or Excel file, or paste the questions as text.")
        except Exception as e:
            log.exception("AI fallback failed softly")
            errors.append(f"AI could not process this file automatically: {e}")

    if adapt_requested and ext != "pdf" and not is_image:
        errors.append("Note: 'Create adapted practice' uses AI vision and works on PDFs/images. This file was processed as a normal extract instead.")

    # ---- Batch categorisation: apply admin-chosen defaults to every question ----
    _overrides: dict = {}
    if type_default: _overrides["type"] = type_default
    if difficulty_default in ("easy", "medium", "hard"): _overrides["difficulty"] = difficulty_default
    if status_default in ("draft", "review", "approved", "archived"): _overrides["status"] = status_default
    if chapter_default: _overrides["chapter"] = chapter_default
    if topic_default: _overrides["topic"] = topic_default
    if section_default: _overrides["section"] = section_default
    if exam_default: _overrides["exam"] = exam_default
    if class_default: _overrides["student_class"] = class_default
    if year_default: _overrides["year"] = year_default
    if marks_default is not None: _overrides["marks"] = marks_default
    if negative_default is not None: _overrides["negative_marks"] = negative_default
    _tags = [t.strip() for t in (tags_default or "").split(",") if t.strip()]
    if _overrides or _tags:
        for q in parsed:
            q.update(_overrides)
            if _tags:
                q["tags"] = sorted(set((q.get("tags") or []) + _tags))

    # ---- Optional separate answer key (uploaded file OR pasted text) ----
    answer_key_applied = 0
    akey_text = (answer_text or "").strip()
    if not akey_text and answer_data:
        try:
            if answer_ext == "docx":
                akey_text = parse_docx(answer_data)
            elif answer_ext == "pdf":
                akey_text = parse_pdf(answer_data)
            elif answer_ext in ("xlsx", "xls"):
                from openpyxl import load_workbook
                import io as _io
                wb = load_workbook(_io.BytesIO(answer_data), data_only=True)
                cells = []
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        cells.append(" ".join("" if c is None else str(c) for c in row))
                akey_text = "\n".join(cells)
            else:
                akey_text = answer_data.decode("utf-8", errors="ignore")
        except Exception as e:
            errors.append(f"Answer file could not be read: {e}")
    if akey_text:
        try:
            from regex_extractor import _extract_answer_key
            key_map = _extract_answer_key(akey_text)
            if key_map:
                for i, q in enumerate(parsed, start=1):
                    if i in key_map:
                        q["correct"] = [key_map[i]]
                        answer_key_applied += 1
                errors.append(f"Answer key: matched {answer_key_applied} of {len(parsed)} question(s) by order.")
            else:
                errors.append("Answer key provided but no entries could be parsed (use '1) A', '1. B', or '1: 2').")
        except Exception as e:
            errors.append(f"Answer key parse error: {e}")

    # duplicate detection against existing bank
    for p in parsed:
        p.setdefault("id_tmp", new_id())
        p.setdefault("source", filename)
        dup = await db.questions.find_one({"text": p.get("text", "")}, {"_id": 0, "id": 1})
        p["duplicate"] = bool(dup)

    if len(parsed) == 0:
        if ext in ("pmd", "p65", "pm6", "pm7"):
            hint = ("This PageMaker file has heavy embedded graphics. Text was extracted but no clear "
                    "numbered questions were detected. Tip: open in PageMaker → export as PDF or "
                    "Word (.docx) → re-upload for best results.")
        elif document_kind != "solutions":
            hint = ("No numbered questions detected. Make sure the file contains 'Q1.' / '1.' style "
                    "numbering, or paste the text directly using the 'Paste text' tab.")
        else:
            hint = None
        if hint: errors.insert(0, hint)

    if len(parsed) > 0:
        _noise = ("no numbered questions detected", "empty document", "no valid question blocks")
        errors = [e for e in errors if not any(n in (e or "").lower() for n in _noise)]

    await _p(98, "Finalising…")
    return {"filename": filename, "file_type": ext, "used_ai": used_ai, "used_regex": used_regex,
            "count": len(parsed), "questions": parsed, "errors": errors, "document_kind": document_kind,
            "answer_key_applied": answer_key_applied}


# Keep strong refs to in-flight background import jobs so the event loop can't GC them.
_bg_tasks: set = set()


async def _run_import_job(job_id: str, kwargs: dict):
    """Background worker: runs the heavy parse and stores the result on the job doc."""
    async def _progress(pct, msg):
        await db.import_jobs.update_one({"id": job_id},
            {"$set": {"progress": {"pct": int(pct), "msg": msg}}})
    try:
        result = await _do_import_parse(progress_cb=_progress, **kwargs)
        await db.import_jobs.update_one({"id": job_id},
            {"$set": {"status": "done", "result": result, "progress": {"pct": 100, "msg": "Done"},
                      "finished_at": now_iso()}})
    except HTTPException as he:
        await db.import_jobs.update_one({"id": job_id},
            {"$set": {"status": "error", "error": he.detail, "finished_at": now_iso()}})
    except Exception as e:
        log.exception("import job failed")
        await db.import_jobs.update_one({"id": job_id},
            {"$set": {"status": "error", "error": str(e), "finished_at": now_iso()}})


@api.post("/import/parse")
async def import_parse(
    file: Optional[UploadFile] = File(None),
    drive_url: Optional[str] = Form(None),
    file_type_hint: Optional[str] = Form(None),
    subject_default: Optional[str] = Form("Physics"),
    raw_text: Optional[str] = Form(None),
    use_ai: Optional[bool] = Form(True),
    import_mode: str = Form("extract"),
    answer_file: Optional[UploadFile] = File(None),
    answer_text: Optional[str] = Form(None),
    type_default: Optional[str] = Form(None),
    difficulty_default: Optional[str] = Form(None),
    status_default: Optional[str] = Form(None),
    chapter_default: Optional[str] = Form(None),
    topic_default: Optional[str] = Form(None),
    section_default: Optional[str] = Form(None),
    exam_default: Optional[str] = Form(None),
    class_default: Optional[str] = Form(None),
    year_default: Optional[str] = Form(None),
    tags_default: Optional[str] = Form(None),
    marks_default: Optional[float] = Form(None),
    negative_default: Optional[float] = Form(None),
    prefer_ai: Optional[bool] = Form(False),
    user: dict = Depends(require_role('admin')),
):
    """Synchronous parse (kept for pasted text / quick jobs). For files that need AI,
    the frontend uses /import/start + /import/jobs to dodge the 60s gateway timeout."""
    data, filename, ext, text = await _resolve_import_inputs(file, drive_url, file_type_hint, raw_text)
    answer_data = None; answer_ext = ""
    if answer_file is not None:
        answer_data = await answer_file.read()
        answer_ext = ((answer_file.filename or "").split(".")[-1] or "").lower()
    return await _do_import_parse(
        data=data, filename=filename, ext=ext, text=text, use_ai=bool(use_ai),
        import_mode=import_mode, subject_default=subject_default or "Physics",
        type_default=type_default, difficulty_default=difficulty_default, status_default=status_default,
        chapter_default=chapter_default, topic_default=topic_default, section_default=section_default,
        exam_default=exam_default, class_default=class_default, year_default=year_default,
        tags_default=tags_default, marks_default=marks_default, negative_default=negative_default,
        answer_data=answer_data, answer_ext=answer_ext, answer_text=answer_text, prefer_ai=bool(prefer_ai))


@api.post("/import/start")
async def import_start(
    file: Optional[UploadFile] = File(None),
    drive_url: Optional[str] = Form(None),
    file_type_hint: Optional[str] = Form(None),
    subject_default: Optional[str] = Form("Physics"),
    raw_text: Optional[str] = Form(None),
    use_ai: Optional[bool] = Form(True),
    import_mode: str = Form("extract"),
    answer_file: Optional[UploadFile] = File(None),
    answer_text: Optional[str] = Form(None),
    type_default: Optional[str] = Form(None),
    difficulty_default: Optional[str] = Form(None),
    status_default: Optional[str] = Form(None),
    chapter_default: Optional[str] = Form(None),
    topic_default: Optional[str] = Form(None),
    section_default: Optional[str] = Form(None),
    exam_default: Optional[str] = Form(None),
    class_default: Optional[str] = Form(None),
    year_default: Optional[str] = Form(None),
    tags_default: Optional[str] = Form(None),
    marks_default: Optional[float] = Form(None),
    negative_default: Optional[float] = Form(None),
    prefer_ai: Optional[bool] = Form(False),
    user: dict = Depends(require_role('admin')),
):
    """Kick off an import in the background and return a job id immediately.
    The AI does the whole job server-side; the client polls /import/jobs/{id}."""
    data, filename, ext, text = await _resolve_import_inputs(file, drive_url, file_type_hint, raw_text)
    answer_data = None; answer_ext = ""
    if answer_file is not None:
        answer_data = await answer_file.read()
        answer_ext = ((answer_file.filename or "").split(".")[-1] or "").lower()
    job_id = new_id()
    await db.import_jobs.insert_one({
        "id": job_id, "status": "processing", "filename": filename,
        "created_by": user["id"], "created_at": now_iso(), "result": None, "error": None,
        "progress": {"pct": 2, "msg": "Queued…"}})
    kwargs = dict(
        data=data, filename=filename, ext=ext, text=text, use_ai=bool(use_ai),
        import_mode=import_mode, subject_default=subject_default or "Physics",
        type_default=type_default, difficulty_default=difficulty_default, status_default=status_default,
        chapter_default=chapter_default, topic_default=topic_default, section_default=section_default,
        exam_default=exam_default, class_default=class_default, year_default=year_default,
        tags_default=tags_default, marks_default=marks_default, negative_default=negative_default,
        answer_data=answer_data, answer_ext=answer_ext, answer_text=answer_text, prefer_ai=bool(prefer_ai))
    task = asyncio.create_task(_run_import_job(job_id, kwargs))
    _bg_tasks.add(task); task.add_done_callback(_bg_tasks.discard)
    return {"job_id": job_id, "filename": filename, "status": "processing"}


@api.get("/import/jobs/{job_id}")
async def import_job_status(job_id: str, user: dict = Depends(require_role('admin'))):
    job = await db.import_jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(404, "Import job not found")
    return job



@api.post("/import/commit")
async def import_commit(payload: dict, user: dict = Depends(require_role('admin'))):
    questions = payload.get("questions", [])
    inserted = []
    skipped: List[dict] = []
    for idx, q in enumerate(questions):
        q.pop("id_tmp", None); q.pop("duplicate", None); q.pop("_include", None)
        try:
            # normalise before validation so AI-emitted quirks don't crash the row
            if isinstance(q.get("correct"), str):
                q["correct"] = [q["correct"]] if q["correct"] else []
            if q.get("correct") is None: q["correct"] = []
            if q.get("options") is None: q["options"] = []
            # coerce numeric strings
            for k in ("marks", "negative_marks"):
                if isinstance(q.get(k), str):
                    try: q[k] = float(q[k].strip())
                    except Exception: q[k] = 4.0 if k == "marks" else 1.0
            # default subject if AI left it blank
            if not q.get("subject"): q["subject"] = payload.get("subject_default") or "Physics"
            # sanity: text must exist
            if not (q.get("text") or "").strip():
                skipped.append({"index": idx, "reason": "empty question text"}); continue
            # difficulty
            if q.get("difficulty") not in ("easy", "medium", "hard"): q["difficulty"] = "medium"
            # type
            if q.get("type") not in ("mcq_single", "mcq_multi", "true_false", "integer",
                                     "assertion_reason", "match", "subjective", "image"):
                q["type"] = "mcq_single"
            # status
            if q.get("status") not in ("draft", "review", "approved", "archived"):
                q["status"] = "approved"

            # uppercase single-letter correct answers for option-based questions
            if q.get("type") in ("mcq_single", "mcq_multi", "true_false", "assertion_reason", "match") and isinstance(q.get("correct"), list):
                q["correct"] = [
                    (c.strip().upper() if isinstance(c, str) and len(c.strip()) == 1 and c.strip().isalpha() else c)
                    for c in q["correct"]
                ]

            m = QuestionIn(**{k: v for k, v in q.items() if k in QuestionIn.model_fields}).model_dump()
            m.update({"id": new_id(), "created_at": now_iso(), "created_by": user["id"],
                      "source": q.get("source", "import")})
            # insert one-by-one so a single bad row can't kill the batch
            try:
                await db.questions.insert_one(dict(m))
                m.pop("_id", None)
                inserted.append(m)
            except Exception as ie:
                skipped.append({"index": idx, "reason": f"db insert: {ie}"})
                log.warning(f"insert skip row {idx}: {ie}")
        except Exception as e:
            skipped.append({"index": idx, "reason": str(e)})
            log.warning(f"commit skip row {idx}: {e}")
    return {"inserted": len(inserted), "skipped": len(skipped), "skipped_details": skipped[:20]}


@api.post("/import/ai-difficulty")
async def import_ai_difficulty(payload: dict, user: dict = Depends(require_role('admin'))):
    texts = payload.get("texts", [])
    results = await ai_predict_difficulty(texts)
    return {"predictions": results}


@api.get("/tests")
async def list_tests(user: dict = Depends(get_current_user)):
    q: dict = {}
    if user["role"] == "student":
        q = {"$or": [{"assigned_to": user["id"]}, {"assigned_to": []}]}
    elif user["role"] == "parent":
        q = {"created_by": user["id"]}
    elif user["role"] == "teacher":
        q = {"created_by": user["id"]}
    else:
        q["personal"] = {"$ne": True}
    docs = await db.tests.find(q, {"_id": 0}).sort("created_at", -1).to_list(300)
    return docs

@api.get("/tests/{tid}")
async def get_test(tid: str, include_questions: bool = False, user: dict = Depends(get_current_user)):
    t = await db.tests.find_one({"id": tid}, {"_id": 0})
    if not t: raise HTTPException(404, "Not found")
    if include_questions:
        qs = await db.questions.find({"id": {"$in": t.get("question_ids", [])}}, {"_id": 0}).to_list(1000)
        order = {qid: i for i, qid in enumerate(t.get("question_ids", []))}
        qs.sort(key=lambda x: order.get(x["id"], 0))
        # Answer-integrity: never expose correct answers to students; reveal
        # explanations only after they have submitted and the test allows it.
        if user["role"] == "student":
            submitted = await db.attempts.find_one(
                {"test_id": tid, "user_id": user["id"], "status": "submitted"}, {"_id": 1})
            reveal_solutions = bool(submitted) and t.get("show_solutions_after", True)
            safe_qs = []
            for q in qs:
                q = dict(q)
                q.pop("correct", None)
                q.pop("hint", None)
                if not reveal_solutions:
                    q.pop("explanation", None)
                    q.pop("explanation_image_url", None)
                safe_qs.append(q)
            qs = safe_qs
        t["questions"] = qs
    return t

@api.post("/tests")
async def create_test(inp: TestIn, user: dict = Depends(get_current_user)):
    if user["role"] not in ("admin", "parent", "teacher"):
        raise HTTPException(403, "Not allowed")
    doc = inp.model_dump()
    total = 0
    if doc["question_ids"]:
        qs = await db.questions.find({"id": {"$in": doc["question_ids"]}}, {"_id": 0, "marks": 1}).to_list(1000)
        total = sum(q.get("marks", 4) for q in qs)
    doc.update({"id": new_id(), "created_at": now_iso(), "created_by": user["id"],
                "created_by_role": user["role"], "total_marks": doc.get("total_marks") or total})
    await db.tests.insert_one(doc)
    return clean(doc)

@api.delete("/tests/{tid}")
async def delete_test(tid: str, user: dict = Depends(require_role('admin'))):
    await db.tests.delete_one({"id": tid})
    return {"ok": True}

@api.post("/tests/random")
async def random_test_builder(payload: dict, user: dict = Depends(get_current_user)):
    if user["role"] not in ("admin", "parent", "teacher"): raise HTTPException(403, "Not allowed")
    q: dict = {"status": "approved"}
    if payload.get("subjects"): q["subject"] = {"$in": payload["subjects"]}
    if payload.get("chapters"): q["chapter"] = {"$in": payload["chapters"]}
    if payload.get("difficulty"): q["difficulty"] = payload["difficulty"]
    n = int(payload.get("count", 10))
    docs = await db.questions.find(q, {"_id": 0}).to_list(1000)
    random.shuffle(docs)
    picked = docs[:n]
    return {"question_ids": [d["id"] for d in picked], "questions": picked}


@api.post("/tests/assign")
async def assign_test(inp: AssignTestIn, user: dict = Depends(require_role('admin'))):
    await db.tests.update_one({"id": inp.test_id}, {"$addToSet": {"assigned_to": {"$each": inp.student_ids}}})
    for sid in inp.student_ids:
        await db.notifications.insert_one({"id": new_id(), "user_id": sid, "type": "test_assigned",
            "message": "New test assigned", "test_id": inp.test_id, "created_at": now_iso(), "read": False})
    return {"ok": True}


@api.post("/tests/parent-assign")
async def parent_assign(inp: ParentAssignIn, user: dict = Depends(require_role('parent'))):
    parent = await db.users.find_one({"id": user["id"]})
    if inp.child_id not in (parent.get("child_ids") or []):
        raise HTTPException(403, "Not your child")
    q = {"status": "approved", "subject": {"$in": inp.subjects}, "difficulty": inp.difficulty}
    docs = await db.questions.find(q, {"_id": 0}).to_list(500)
    random.shuffle(docs)
    picked = docs[:inp.num_questions]
    total = sum(d.get("marks", 4) for d in picked)
    t = {"id": new_id(), "title": inp.title, "exam_type": "parent_custom",
         "description": f"Assigned by parent — {inp.difficulty} level",
         "subjects": inp.subjects, "duration_minutes": inp.duration_minutes,
         "total_marks": total, "negative_marking": True, "shuffle_questions": True,
         "shuffle_options": False, "show_solutions_after": True,
         "question_ids": [d["id"] for d in picked], "sections": [],
         "assigned_to": [inp.child_id], "created_by": user["id"],
         "created_by_role": "parent", "created_at": now_iso()}
    await db.tests.insert_one(t)
    await db.notifications.insert_one({"id": new_id(), "user_id": inp.child_id, "type": "parent_test",
        "message": f"Your parent assigned a new test: {inp.title}", "test_id": t["id"],
        "created_at": now_iso(), "read": False})
    t.pop("_id", None)
    return t


@api.post("/attempts/start")
async def start_attempt(inp: AttemptStartIn, user: dict = Depends(require_role('student'))):
    t = await db.tests.find_one({"id": inp.test_id}, {"_id": 0})
    if not t: raise HTTPException(404, "Test not found")
    existing = await db.attempts.find_one({"test_id": inp.test_id, "user_id": user["id"], "status": "in_progress"}, {"_id": 0})
    if existing: return existing
    # One-attempt lock: a completed test can only be reattempted with a Retake Pass.
    submitted = await db.attempts.find_one({"test_id": inp.test_id, "user_id": user["id"], "status": "submitted"}, {"_id": 0})
    if submitted:
        if not inp.use_retake_pass:
            return submitted
        fresh = await db.users.find_one({"id": user["id"]})
        if fresh.get("retake_passes", 0) <= 0:
            raise HTTPException(400, "No Retake Pass available. Buy one in the Coin Store.")
        await db.users.update_one({"id": user["id"]}, {"$inc": {"retake_passes": -1}})
        await db.notifications.insert_one({"id": new_id(), "user_id": user["id"], "type": "store",
            "message": "🎟️ Retake Pass used — good luck on your reattempt!", "created_at": now_iso(), "read": False})
    a = {"id": new_id(), "test_id": inp.test_id, "user_id": user["id"],
         "started_at": now_iso(),
         "ends_at": (datetime.now(timezone.utc) + timedelta(minutes=t["duration_minutes"])).isoformat(),
         "status": "in_progress", "answers": [], "score": 0, "correct": 0, "wrong": 0, "unattempted": 0}
    await db.attempts.insert_one(a)
    a.pop("_id", None)
    return a


@api.post("/attempts/submit")
async def submit_attempt(inp: AttemptSubmitIn, request: Request, user: dict = Depends(require_role('student'))):
    await rate_limit(f"submit:{user['id']}", limit=20, window_seconds=60)
    a = await db.attempts.find_one({"id": inp.attempt_id, "user_id": user["id"]}, {"_id": 0})
    if not a: raise HTTPException(404, "Attempt not found")
    if a["status"] == "submitted": return a
    # Server-side time-limit enforcement: the deadline is authoritative and set
    # at start (ends_at). A client that pauses JS cannot extend it.
    now = datetime.now(timezone.utc)
    late_submission = False
    time_taken_seconds = None
    try:
        ends_at = datetime.fromisoformat(a["ends_at"])
        started_at = datetime.fromisoformat(a["started_at"])
        time_taken_seconds = int((now - started_at).total_seconds())
        if now > ends_at + timedelta(seconds=120):  # 120s network grace
            late_submission = True
            log.warning("Late submission attempt=%s user=%s late_by=%ss",
                        inp.attempt_id, user["id"], int((now - ends_at).total_seconds()))
    except Exception:
        pass
    t = await db.tests.find_one({"id": a["test_id"]}, {"_id": 0})
    qs = await db.questions.find({"id": {"$in": t["question_ids"]}}, {"_id": 0}).to_list(1000)
    qmap = {q["id"]: q for q in qs}
    score = 0.0; correct = 0; wrong = 0; unattempted = 0
    detailed = []; subject_stats: dict = {}
    ans_map = {ans.question_id: ans for ans in inp.answers}
    for ans in inp.answers:
        q = qmap.get(ans.question_id)
        if not q: continue
        sub = q.get("subject", "Other")
        subject_stats.setdefault(sub, {"correct": 0, "wrong": 0, "total": 0, "score": 0})
        subject_stats[sub]["total"] += 1
        user_ans = sorted([str(x).strip().lower() for x in ans.answer])
        correct_ans = sorted([str(x).strip().lower() for x in q.get("correct", [])])
        is_correct = user_ans and user_ans == correct_ans
        is_empty = len(user_ans) == 0 and not ans.image_answer
        if is_empty:
            unattempted += 1
            detailed.append({"question_id": q["id"], "user_answer": ans.answer, "correct": correct_ans, "result": "unattempted", "marks_awarded": 0, "confidence": ans.confidence, "time_taken": ans.time_taken, "difficulty": q.get("difficulty")})
        elif is_correct:
            m = q.get("marks", 4); score += m; correct += 1
            subject_stats[sub]["correct"] += 1; subject_stats[sub]["score"] += m
            detailed.append({"question_id": q["id"], "user_answer": ans.answer, "correct": correct_ans, "result": "correct", "marks_awarded": m, "confidence": ans.confidence, "image_answer": ans.image_answer, "time_taken": ans.time_taken, "difficulty": q.get("difficulty")})
        else:
            # subjective + image submitted → needs manual grading
            if q.get("type") == "subjective" or ans.image_answer:
                detailed.append({"question_id": q["id"], "user_answer": ans.answer, "correct": correct_ans, "result": "pending_grading", "marks_awarded": 0, "confidence": ans.confidence, "image_answer": ans.image_answer, "time_taken": ans.time_taken, "difficulty": q.get("difficulty")})
                continue
            m = -q.get("negative_marks", 1) if t.get("negative_marking") else 0
            score += m; wrong += 1
            subject_stats[sub]["wrong"] += 1
            detailed.append({"question_id": q["id"], "user_answer": ans.answer, "correct": correct_ans, "result": "wrong", "marks_awarded": m, "confidence": ans.confidence, "image_answer": ans.image_answer, "time_taken": ans.time_taken, "difficulty": q.get("difficulty")})
    seen = {ans.question_id for ans in inp.answers}
    for qid in t["question_ids"]:
        if qid not in seen:
            unattempted += 1
            q = qmap.get(qid)
            if q:
                sub = q.get("subject", "Other")
                subject_stats.setdefault(sub, {"correct": 0, "wrong": 0, "total": 0, "score": 0})
                subject_stats[sub]["total"] += 1
                detailed.append({"question_id": qid, "user_answer": [], "correct": sorted([str(x).strip().lower() for x in q.get("correct", [])]), "result": "unattempted", "marks_awarded": 0, "difficulty": q.get("difficulty")})
    upd = {"status": "submitted", "submitted_at": now_iso(),
           "answers": [ans.model_dump() for ans in inp.answers],
           "score": round(score, 2), "correct": correct, "wrong": wrong, "unattempted": unattempted,
           "late_submission": late_submission, "time_taken_seconds": time_taken_seconds,
           "total_marks": t.get("total_marks", 0), "detailed": detailed, "subject_stats": subject_stats}
    await db.attempts.update_one({"id": inp.attempt_id}, {"$set": upd})
    await sync_reviews_from_attempt(user["id"], detailed, qmap)
    # Personal (self-made) quizzes don't award coins — keeps the leaderboard fair.
    bonus = 0
    if not t.get("personal"):
        coins = int(max(score, 0))
        # Date-aware streak: only count one study-day per calendar day.
        today = now.date()
        last_day = user.get("last_study_date")
        cur_streak = int(user.get("streak_days", 0) or 0)
        if last_day == today.isoformat():
            new_streak = cur_streak or 1
        elif last_day == (today - timedelta(days=1)).isoformat():
            new_streak = cur_streak + 1
        else:
            new_streak = 1
        # Streak milestones: bonus coins + a permanent badge.
        MILESTONES = {3: (0, "3-Day Streak", "flame"), 7: (50, "Week Warrior", "trophy"),
                      30: (200, "Month Master", "medal"), 100: (500, "Century Streak", "crown")}
        badge = None
        if new_streak != cur_streak and new_streak in MILESTONES:
            bonus, bname, bicon = MILESTONES[new_streak]
            badge = {"name": bname, "icon": bicon, "milestone": new_streak}
        upd = {"$inc": {"reward_coins": coins + bonus},
               "$set": {"streak_days": new_streak, "last_study_date": today.isoformat(), "last_active": now_iso()}}
        if badge:
            upd["$addToSet"] = {"earned_badges": badge}
        await db.users.update_one({"id": user["id"]}, upd)
        if badge:
            msg = f"🔥 {new_streak}-day streak! Earned the '{badge['name']}' badge" + (f" +{bonus} coins" if bonus else "")
            await db.notifications.insert_one({"id": new_id(), "user_id": user["id"], "type": "streak_bonus",
                "message": msg, "created_at": now_iso(), "read": False})
    final = await db.attempts.find_one({"id": inp.attempt_id}, {"_id": 0})
    # Post-submit emails & alerts (fire-and-forget, never blocks submit).
    try:
        peers = await db.attempts.find({"test_id": a["test_id"], "status": "submitted"}, {"_id": 0, "score": 1}).to_list(5000)
        total_peers = len(peers)
        beat = sum(1 for s in peers if final.get("score", 0) > s.get("score", 0))
        rank = {"percentile": round(beat / total_peers * 100) if total_peers else 50, "beat": beat, "total": total_peers}
        top_score = max((s.get("score", 0) for s in peers), default=final.get("score", 0))
        topper = {"top_score": top_score, "total": final.get("total_marks", 0)}
        this_pct = (final.get("score", 0) / max(final.get("total_marks", 1), 1)) * 100
        # 1) Result email to the candidate (respects toggle) with a retake CTA
        if user.get("email_results_enabled", True):
            from email_utils import send_result_email
            app_url = os.environ.get("APP_BASE_URL", "").rstrip("/")
            retake_url = f"{app_url}/student/exam/{a['test_id']}" if app_url else None
            coach_input = dict(final); coach_input["test_title"] = t.get("title", "")
            async def _coach_then_email():
                coach = await generate_coach(coach_input, qmap)
                await send_result_email(user, t, final, qmap, rank, topper, retake_url, coach)
            asyncio.create_task(_coach_then_email())
        else:
            coach_input = dict(final); coach_input["test_title"] = t.get("title", "")
            asyncio.create_task(generate_coach(coach_input, qmap))
        # 2) Parent alert on a sharp score drop vs the student's recent average
        prior = await db.attempts.find(
            {"user_id": user["id"], "status": "submitted", "id": {"$ne": final["id"]}},
            {"_id": 0, "score": 1, "total_marks": 1}).sort("submitted_at", -1).limit(5).to_list(5)
        if prior and (user.get("parent_ids") or []):
            prior_pcts = [(p.get("score", 0) / max(p.get("total_marks", 1), 1)) * 100 for p in prior]
            prior_avg = sum(prior_pcts) / len(prior_pcts)
            drop = prior_avg - this_pct
            from email_utils import send_parent_alert_email
            parents = await db.users.find({"id": {"$in": user.get("parent_ids") or []}}, {"_id": 0}).to_list(20)
            for p in parents:
                if p.get("alert_mode", "instant") == "weekly":
                    continue
                if drop >= int(p.get("alert_drop_threshold", 15)):
                    asyncio.create_task(send_parent_alert_email(p, user, t, round(this_pct), round(prior_avg)))
    except Exception as e:
        log.warning("Post-submit email/alerts failed: %s", e)
    return final


@api.get("/attempts")
async def list_attempts(user_id: Optional[str] = None, test_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    q: dict = {}
    if user["role"] == "student": q["user_id"] = user["id"]
    elif user_id: q["user_id"] = user_id
    if test_id: q["test_id"] = test_id
    return await db.attempts.find(q, {"_id": 0}).sort("started_at", -1).to_list(500)

@api.get("/attempts/{aid}")
async def get_attempt(aid: str, user: dict = Depends(get_current_user)):
    a = await db.attempts.find_one({"id": aid}, {"_id": 0})
    if not a: raise HTTPException(404, "Not found")
    if user["role"] == "student" and a["user_id"] != user["id"]:
        raise HTTPException(403, "Not yours")
    return a


# ---------- Trust Score proctoring ----------
PROCTOR_WEIGHTS = {"tab_switch": 12, "fullscreen_exit": 10, "copy": 8, "paste": 8, "window_blur": 6, "idle": 4, "right_click": 3}


class ProctorEventIn(BaseModel):
    type: str


@api.post("/attempts/{attempt_id}/proctor-event")
async def proctor_event(attempt_id: str, inp: ProctorEventIn, user: dict = Depends(require_role('student'))):
    a = await db.attempts.find_one({"id": attempt_id, "user_id": user["id"], "status": "in_progress"}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Active attempt not found")
    if inp.type not in PROCTOR_WEIGHTS:
        return {"ok": True, "trust_score": a.get("trust_score", 100)}
    counts = a.get("violation_counts") or {}
    counts[inp.type] = counts.get(inp.type, 0) + 1
    total = sum(PROCTOR_WEIGHTS.get(k, 0) * v for k, v in counts.items())
    trust = max(0, 100 - total)
    t = await db.tests.find_one({"id": a["test_id"]}, {"_id": 0, "proctor_cutoff": 1, "proctor_action": 1})
    cutoff = int((t or {}).get("proctor_cutoff", 0) or 0)
    action = None
    upd_set = {"violation_counts": counts, "trust_score": trust}
    if cutoff > 0 and trust <= cutoff:
        upd_set["flagged"] = True
        action = (t or {}).get("proctor_action", "flag") or "flag"
    await db.attempts.update_one({"id": attempt_id}, {
        "$set": upd_set,
        "$push": {"proctor_events": {"$each": [{"type": inp.type, "at": now_iso()}], "$slice": -300}},
    })
    return {"ok": True, "trust_score": trust, "violations": sum(counts.values()), "action": action}


@api.get("/admin/proctoring")
async def admin_proctoring(test_id: Optional[str] = None, user: dict = Depends(require_role('admin'))):
    q: dict = {}
    if test_id:
        q["test_id"] = test_id
    attempts = await db.attempts.find(q, {"_id": 0, "detailed": 0, "proctor_events": 0, "answers": 0}).sort("started_at", -1).to_list(500)
    uids = list({a["user_id"] for a in attempts})
    tids = list({a["test_id"] for a in attempts})
    users = {u["id"]: u for u in await db.users.find({"id": {"$in": uids}}, {"_id": 0, "id": 1, "name": 1, "email": 1}).to_list(2000)}
    tests = {t["id"]: t.get("title") for t in await db.tests.find({"id": {"$in": tids}}, {"_id": 0, "id": 1, "title": 1}).to_list(2000)}
    rows = []
    for a in attempts:
        vc = a.get("violation_counts") or {}
        rows.append({
            "attempt_id": a["id"], "status": a.get("status"),
            "student": users.get(a["user_id"], {}).get("name", "Unknown"),
            "email": users.get(a["user_id"], {}).get("email"),
            "test": tests.get(a["test_id"], "Unknown"),
            "trust_score": a.get("trust_score", 100), "violation_counts": vc,
            "total_violations": sum(vc.values()),
            "submitted_at": a.get("submitted_at"), "started_at": a.get("started_at"),
        })
    rows.sort(key=lambda r: r["trust_score"])
    return {"weights": PROCTOR_WEIGHTS, "rows": rows}


@api.get("/analytics/admin")
async def admin_analytics(user: dict = Depends(require_role('admin'))):
    students = await db.users.count_documents({"role": "student"})
    parents = await db.users.count_documents({"role": "parent"})
    q_count = await db.questions.count_documents({})
    tests = await db.tests.count_documents({})
    attempts = await db.attempts.count_documents({"status": "submitted"})
    all_attempts = await db.attempts.find({"status": "submitted"}, {"_id": 0}).to_list(2000)
    subject_totals: dict = {}
    for a in all_attempts:
        for s, st in (a.get("subject_stats") or {}).items():
            subject_totals.setdefault(s, {"score": 0, "count": 0})
            subject_totals[s]["score"] += st.get("score", 0)
            subject_totals[s]["count"] += 1
    subject_avg = [{"subject": s, "avg_score": round(v["score"] / max(v["count"], 1), 2), "attempts": v["count"]} for s, v in subject_totals.items()]
    missed: dict = {}
    for a in all_attempts:
        for d in (a.get("detailed") or []):
            if d["result"] == "wrong":
                missed[d["question_id"]] = missed.get(d["question_id"], 0) + 1
    top_missed_ids = sorted(missed.items(), key=lambda x: -x[1])[:5]
    missed_qs = []
    if top_missed_ids:
        qmap = {q["id"]: q for q in await db.questions.find(
            {"id": {"$in": [qid for qid, _ in top_missed_ids]}}, {"_id": 0}).to_list(5)}
        for qid, cnt in top_missed_ids:
            if qid in qmap: missed_qs.append({"question": qmap[qid], "wrong_count": cnt})
    top_users = await db.users.find({"role": "student"}, {"_id": 0, "password": 0}).sort("reward_coins", -1).limit(5).to_list(5)
    return {"students": students, "parents": parents, "questions": q_count, "tests": tests, "attempts": attempts,
            "subject_avg": subject_avg, "top_missed": missed_qs, "top_performers": top_users}


@api.get("/analytics/student/{sid}")
async def student_analytics(sid: str, user: dict = Depends(get_current_user)):
    if user["role"] == "student" and user["id"] != sid:
        raise HTTPException(403, "Not yours")
    attempts = await db.attempts.find({"user_id": sid, "status": "submitted"}, {"_id": 0}).sort("submitted_at", 1).to_list(500)
    subj_agg: dict = {}
    for a in attempts:
        for s, st in (a.get("subject_stats") or {}).items():
            subj_agg.setdefault(s, {"correct": 0, "wrong": 0, "total": 0, "score": 0})
            for k in ("correct", "wrong", "total", "score"):
                subj_agg[s][k] += st.get(k, 0)
    trend = [{"attempt_id": a["id"], "date": (a.get("submitted_at") or "")[:10],
              "score": a.get("score", 0), "total": a.get("total_marks", 0),
              "accuracy": round((a.get("correct", 0) / max(a.get("correct", 0) + a.get("wrong", 0), 1)) * 100, 1)} for a in attempts]
    all_students = await db.users.find({"role": "student"}, {"_id": 0, "id": 1, "reward_coins": 1}).sort("reward_coins", -1).to_list(1000)
    rank = next((i + 1 for i, u in enumerate(all_students) if u["id"] == sid), None)
    wrong_ids = []
    for a in attempts:
        for d in (a.get("detailed") or []):
            if d["result"] == "wrong": wrong_ids.append(d["question_id"])
    wrong_ids = list(dict.fromkeys(wrong_ids))[:20]
    wrong_qs = await db.questions.find({"id": {"$in": wrong_ids}}, {"_id": 0}).to_list(20)
    total_correct = sum(a.get("correct", 0) for a in attempts)
    total_wrong = sum(a.get("wrong", 0) for a in attempts)
    accuracy = round(total_correct / max(total_correct + total_wrong, 1) * 100, 1)
    return {"attempts": len(attempts), "accuracy": accuracy, "subject_stats": subj_agg,
            "trend": trend, "rank": rank, "total_students": len(all_students),
            "wrong_questions": wrong_qs}


@api.get("/analytics/parent/{child_id}")
async def parent_analytics(child_id: str, user: dict = Depends(require_role('parent'))):
    parent = await db.users.find_one({"id": user["id"]})
    if child_id not in (parent.get("child_ids") or []):
        raise HTTPException(403, "Not your child")
    return await student_analytics(child_id, parent)


@api.get("/leaderboard")
async def leaderboard(kind: str = "coins", user: dict = Depends(get_current_user)):
    if kind == "referral":
        pipeline = [{"$group": {"_id": "$referrer_id", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}}, {"$limit": 10}]
        agg = await db.referrals.aggregate(pipeline).to_list(10)
        ids = [row["_id"] for row in agg]
        users = await db.users.find({"id": {"$in": ids}}, {"_id": 0, "password": 0}).to_list(10)
        umap = {u["id"]: u for u in users}
        out = [{"user": umap[row["_id"]], "count": row["count"]} for row in agg if row["_id"] in umap]
        return out
    docs = await db.users.find({"role": "student"}, {"_id": 0, "password": 0}).sort("reward_coins", -1).limit(10).to_list(10)
    return [{"user": u, "count": u.get("reward_coins", 0)} for u in docs]


class HintIn(BaseModel):
    question_id: str


@api.post("/attempts/{attempt_id}/hint")
async def use_hint(attempt_id: str, inp: HintIn, user: dict = Depends(require_role('student'))):
    """Spend one Hint Token to reveal a clue for a question mid-test. Free if already unlocked."""
    a = await db.attempts.find_one({"id": attempt_id, "user_id": user["id"], "status": "in_progress"}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Active attempt not found")
    q = await db.questions.find_one({"id": inp.question_id}, {"_id": 0})
    if not q:
        raise HTTPException(404, "Question not found")

    def build_hint(qd: dict) -> str:
        if qd.get("hint"):
            return str(qd["hint"])
        exp = (qd.get("explanation") or "").strip()
        if exp:
            first = exp.replace("\n", " ").split(". ")[0]
            return (first[:160] + ("…" if len(first) > 160 else ""))
        return f"Focus on the core concept of {qd.get('chapter') or qd.get('subject') or 'this topic'} and eliminate options that break the basic rule."

    already = inp.question_id in (a.get("hints_used") or [])
    if already:
        fresh = await db.users.find_one({"id": user["id"]}, {"_id": 0, "hint_tokens": 1})
        return {"hint": build_hint(q), "hint_tokens_left": fresh.get("hint_tokens", 0), "charged": False}

    fresh = await db.users.find_one({"id": user["id"]})
    if fresh.get("hint_tokens", 0) <= 0:
        raise HTTPException(400, "No Hint Tokens left. Buy some in the Coin Store.")
    await db.users.update_one({"id": user["id"]}, {"$inc": {"hint_tokens": -1}})
    await db.attempts.update_one({"id": attempt_id}, {"$addToSet": {"hints_used": inp.question_id}})
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0, "hint_tokens": 1})
    return {"hint": build_hint(q), "hint_tokens_left": u.get("hint_tokens", 0), "charged": True}


@api.get("/notifications")
async def notifs(user: dict = Depends(get_current_user)):
    return await db.notifications.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).limit(50).to_list(50)

@api.post("/notifications/read/{nid}")
async def mark_read(nid: str, user: dict = Depends(get_current_user)):
    await db.notifications.update_one({"id": nid, "user_id": user["id"]}, {"$set": {"read": True}})
    return {"ok": True}


@api.post("/announcements")
async def create_announcement(inp: AnnouncementIn, user: dict = Depends(require_role('admin'))):
    a = inp.model_dump()
    a.update({"id": new_id(), "created_at": now_iso(), "created_by": user["id"]})
    await db.announcements.insert_one(a)
    q = {} if inp.audience == "all" else ({"role": "student"} if inp.audience == "students" else {"role": "parent"})
    users = await db.users.find(q, {"_id": 0, "id": 1}).to_list(2000)
    if users:
        notes = [{"id": new_id(), "user_id": u["id"], "type": "announcement", "message": inp.title,
                  "created_at": now_iso(), "read": False} for u in users]
        await db.notifications.insert_many(notes)
    a.pop("_id", None)
    return a

@api.get("/announcements")
async def list_announcements(user: dict = Depends(get_current_user)):
    return await db.announcements.find({}, {"_id": 0}).sort("created_at", -1).limit(50).to_list(50)


@api.get("/analytics/test-percentile/{tid}")
async def test_percentile(tid: str, score: float, user: dict = Depends(get_current_user)):
    """Return the percentile of `score` among all submitted attempts on this test."""
    scores = await db.attempts.find({"test_id": tid, "status": "submitted"}, {"_id": 0, "score": 1}).to_list(5000)
    total = len(scores)
    if total == 0: return {"percentile": 50, "beat": 0, "total": 0}
    beat = sum(1 for s in scores if score > s.get("score", 0))
    pct = round(beat / total * 100)
    return {"percentile": pct, "beat": beat, "total": total}


@api.post("/rewards/freeze/use")
async def use_freeze(user: dict = Depends(get_current_user)):
    """Consume the monthly free freeze, or a purchased one if the monthly is used."""
    u = await db.users.find_one({"id": user["id"]})
    last = u.get("last_freeze_used_at")
    now = datetime.now(timezone.utc)
    monthly_used = False
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            monthly_used = (last_dt.year == now.year and last_dt.month == now.month)
        except ValueError:
            pass
    if not monthly_used:
        await db.users.update_one({"id": user["id"]}, {"$set": {"last_freeze_used_at": now.isoformat()}, "$inc": {"streak_days": 1}})
        return {"ok": True, "message": "Streak saved for today!"}
    if u.get("streak_freezes", 0) > 0:
        await db.users.update_one({"id": user["id"]}, {"$inc": {"streak_freezes": -1, "streak_days": 1}})
        return {"ok": True, "message": "Streak saved using a purchased freeze!"}
    raise HTTPException(400, "No freeze available. Buy one in the Coin Store.")


@api.get("/rewards/me")
async def my_rewards(user: dict = Depends(get_current_user)):
    refs = await db.referrals.count_documents({"referrer_id": user["id"]})
    badges = []
    if user.get("reward_coins", 0) >= 500: badges.append({"name": "500 Club", "icon": "medal"})
    if refs >= 3: badges.append({"name": "Influencer", "icon": "sparkles"})
    # Permanent streak-milestone badges earned over time.
    names = {b["name"] for b in badges}
    for b in (user.get("earned_badges", []) or []):
        if b.get("name") not in names:
            badges.append({"name": b["name"], "icon": b.get("icon", "sparkles")})
            names.add(b["name"])
    # freeze availability: allowed once per calendar month
    last = user.get("last_freeze_used_at")
    freeze_available = True
    if last:
        try:
            now = datetime.now(timezone.utc); last_dt = datetime.fromisoformat(last)
            if last_dt.year == now.year and last_dt.month == now.month:
                freeze_available = False
        except ValueError:
            pass
    return {"coins": user.get("reward_coins", 0), "streak": user.get("streak_days", 0),
            "referrals": refs, "referral_code": user.get("referral_code"), "badges": badges,
            "freeze_available": freeze_available, "last_freeze_used_at": last,
            "streak_freezes": user.get("streak_freezes", 0),
            "hint_tokens": user.get("hint_tokens", 0),
            "retake_passes": user.get("retake_passes", 0)}


STORE_ITEMS = [
    {"id": "streak_freeze", "name": "Streak Freeze", "desc": "Save your streak on a day you miss practice.", "cost": 120, "icon": "snowflake", "field": "streak_freezes"},
    {"id": "hint_token", "name": "Hint Token", "desc": "Reveal a helpful hint on a tough question.", "cost": 60, "icon": "lightbulb", "field": "hint_tokens"},
    {"id": "retake_pass", "name": "Retake Pass", "desc": "Reattempt a completed test one more time.", "cost": 200, "icon": "repeat", "field": "retake_passes"},
]


@api.get("/store")
async def get_store(user: dict = Depends(get_current_user)):
    return {"items": STORE_ITEMS, "coins": user.get("reward_coins", 0),
            "inventory": {i["field"]: user.get(i["field"], 0) for i in STORE_ITEMS}}


class BuyIn(BaseModel):
    item_id: str


@api.post("/store/buy")
async def buy_item(inp: BuyIn, user: dict = Depends(get_current_user)):
    item = next((i for i in STORE_ITEMS if i["id"] == inp.item_id), None)
    if not item:
        raise HTTPException(404, "Item not found")
    fresh = await db.users.find_one({"id": user["id"]})
    if fresh.get("reward_coins", 0) < item["cost"]:
        raise HTTPException(400, "Not enough coins")
    await db.users.update_one({"id": user["id"]}, {"$inc": {"reward_coins": -item["cost"], item["field"]: 1}})
    await db.notifications.insert_one({"id": new_id(), "user_id": user["id"], "type": "store",
        "message": f"🛍️ Purchased {item['name']} for {item['cost']} coins", "created_at": now_iso(), "read": False})
    u = await db.users.find_one({"id": user["id"]})
    return {"ok": True, "coins": u.get("reward_coins", 0),
            "inventory": {i["field"]: u.get(i["field"], 0) for i in STORE_ITEMS}}


# ---------- Peer Duel ----------
class DuelCreateIn(BaseModel):
    test_id: str
    attempt_id: str

@api.post("/duels")
async def create_duel(inp: DuelCreateIn, user: dict = Depends(require_role('student'))):
    a = await db.attempts.find_one({"id": inp.attempt_id, "user_id": user["id"], "status": "submitted"}, {"_id": 0})
    if not a: raise HTTPException(404, "Attempt not found or not yours")
    t = await db.tests.find_one({"id": inp.test_id}, {"_id": 0})
    if not t: raise HTTPException(404, "Test not found")
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    d = {
        "id": new_id(), "code": code, "test_id": inp.test_id, "test_title": t["title"],
        "challenger_id": user["id"], "challenger_name": user["name"],
        "challenger_score": a["score"], "challenger_total": a["total_marks"],
        "challenger_attempt": inp.attempt_id, "opponents": [],
        "created_at": now_iso(),
    }
    await db.duels.insert_one(d)
    d.pop("_id", None)
    return d

@api.get("/duels/{code}")
async def get_duel(code: str):
    d = await db.duels.find_one({"code": code.upper()}, {"_id": 0})
    if not d: raise HTTPException(404, "Duel not found")
    return d

@api.post("/duels/{code}/complete")
async def complete_duel(code: str, payload: dict, user: dict = Depends(require_role('student'))):
    attempt_id = payload.get("attempt_id")
    a = await db.attempts.find_one({"id": attempt_id, "user_id": user["id"], "status": "submitted"}, {"_id": 0})
    if not a: raise HTTPException(404, "Attempt not found")
    entry = {"user_id": user["id"], "user_name": user["name"], "score": a["score"], "total": a["total_marks"], "attempt_id": attempt_id, "at": now_iso()}
    await db.duels.update_one({"code": code.upper()}, {"$addToSet": {"opponents": entry}})
    return await db.duels.find_one({"code": code.upper()}, {"_id": 0})


# ---------- Wrong-only Retest ----------
@api.post("/attempts/{aid}/wrong-retest")
async def wrong_retest(aid: str, user: dict = Depends(require_role('student'))):
    a = await db.attempts.find_one({"id": aid, "user_id": user["id"]}, {"_id": 0})
    if not a: raise HTTPException(404, "Attempt not found")
    wrong_ids = [d["question_id"] for d in (a.get("detailed") or []) if d["result"] == "wrong"]
    if not wrong_ids: raise HTTPException(400, "No wrong questions to retest — you nailed it!")
    qs = await db.questions.find({"id": {"$in": wrong_ids}}, {"_id": 0, "marks": 1}).to_list(500)
    total = sum(q.get("marks", 4) for q in qs)
    t = {
        "id": new_id(), "title": f"Wrong-only drill · {len(wrong_ids)} Qs",
        "exam_type": "wrong_retest",
        "description": "Retest of the questions you missed last time.",
        "subjects": [], "duration_minutes": max(5, len(wrong_ids) * 2),
        "total_marks": total, "negative_marking": False,
        "shuffle_questions": True, "shuffle_options": False,
        "show_solutions_after": True, "question_ids": wrong_ids, "sections": [],
        "assigned_to": [user["id"]], "created_by": user["id"], "created_by_role": "student",
        "created_at": now_iso(),
    }
    await db.tests.insert_one(t)
    t.pop("_id", None)
    return t


# ---------- Chapter Podcasts (AI-scripted) ----------
@api.post("/podcasts/script")
async def podcast_script(payload: dict, user: dict = Depends(get_current_user)):
    subject = payload.get("subject", "Physics")
    chapter = payload.get("chapter", "General")
    key, _ai_prov, _ai_model = resolve_ai()
    fallback = f"Welcome to your 3-minute {chapter} refresher. This is a quick summary of the key ideas in {chapter} from {subject}. Practice a few MCQs on this chapter today to lock it in. That's your daily win."
    if not key:
        return {"script": fallback, "used_ai": False}
    try:
        text = await ai_complete(
            ("You script 3-minute (about 450-word) audio revision podcasts for Indian JEE/NEET aspirants. "
             "Warm, energetic, spoken-word tone. NO markdown, NO headings, NO bullets. Plain prose that flows when read aloud. "
             "Cover: 1 warm hook, 3 key concepts with simple examples, one common exam mistake, one closing motivation."),
            f"Subject: {subject}. Chapter: {chapter}. Write the podcast.")
        text = re.sub(r"\*+", "", text).strip()
        return {"script": text or fallback, "used_ai": True}
    except Exception as e:
        log.warning(f"podcast ai fail: {e}")
        return {"script": fallback, "used_ai": False}


@api.get("/podcasts/chapters")
async def podcast_chapters(user: dict = Depends(get_current_user)):
    pipeline = [
        {"$match": {"chapter": {"$ne": ""}}},
        {"$group": {"_id": {"subject": "$subject", "chapter": "$chapter"}, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    agg = await db.questions.aggregate(pipeline).to_list(200)
    return [{"subject": r["_id"]["subject"], "chapter": r["_id"]["chapter"], "questions": r["count"]} for r in agg]


# ---------- Parent Weekly Digest ----------
async def build_child_digest(child_id: str):
    """Compute a child's 7-day practice digest. Shared by the parent API and the weekly cron."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    child = await db.users.find_one({"id": child_id}, {"_id": 0, "password": 0})
    if not child:
        return None
    all_attempts = await db.attempts.find({"user_id": child_id, "status": "submitted"}, {"_id": 0}).sort("submitted_at", -1).to_list(200)
    this_week = [a for a in all_attempts if a.get("submitted_at") and datetime.fromisoformat(a["submitted_at"]) >= week_ago]
    prior = [a for a in all_attempts if a not in this_week][:5]
    subj_agg = {}
    for a in this_week:
        for s, st in (a.get("subject_stats") or {}).items():
            subj_agg.setdefault(s, {"correct": 0, "wrong": 0, "total": 0})
            for k in ("correct", "wrong", "total"):
                subj_agg[s][k] += st.get(k, 0)
    weak = None; weak_acc = 100
    for s, st in subj_agg.items():
        acc = st["correct"] / max(st["total"], 1) * 100
        if acc < weak_acc: weak_acc = acc; weak = s
    def avg_score(lst):
        vs = [a.get("score", 0) / max(a.get("total_marks", 1), 1) * 100 for a in lst]
        return round(sum(vs) / max(len(vs), 1), 1) if vs else 0
    this_avg = avg_score(this_week); prev_avg = avg_score(prior)
    delta = round(this_avg - prev_avg, 1)
    tids = list({a.get("test_id") for a in this_week})
    tdocs = await db.tests.find({"id": {"$in": tids}}, {"_id": 0, "id": 1, "title": 1}).to_list(50)
    tmap = {t["id"]: t.get("title", "Test") for t in tdocs}
    # Biggest score drops within the week (attempt-to-attempt), for the digest recap.
    tw_sorted = sorted(this_week, key=lambda a: (a.get("submitted_at") or ""))
    drops = []
    prev = None
    for a in tw_sorted:
        pct = (a.get("score", 0) / max(a.get("total_marks", 1), 1)) * 100
        if prev is not None and pct < prev - 0.5:
            drops.append({"title": tmap.get(a.get("test_id")) or "Test", "drop": round(prev - pct, 1), "pct": round(pct)})
        prev = pct
    drops.sort(key=lambda d: -d["drop"])
    drops = drops[:3]
    return {
        "child": {"id": child["id"], "name": child["name"], "avatar": child.get("avatar"), "exam_target": child.get("exam_target")},
        "week_attempts": len(this_week),
        "week_avg_pct": this_avg,
        "prev_avg_pct": prev_avg,
        "delta_pct": delta,
        "weak_subject": weak,
        "weak_accuracy": round(weak_acc, 1) if weak else None,
        "streak_days": child.get("streak_days", 0),
        "recent": [{"title": tmap.get(a.get("test_id"), "Test"), "score": a.get("score"), "total": a.get("total_marks"),
                    "date": (a.get("submitted_at") or "")[:10]} for a in this_week[:5]],
        "drops": drops,
        "message": (
            f"{child['name']} took {len(this_week)} mocks this week and is trending "
            + ("up 📈" if delta > 0 else "down 📉" if delta < 0 else "steady ➡️")
            + f" ({delta:+.1f} pp). "
            + (f"Weak spot: {weak} at {weak_acc:.0f}% accuracy." if weak else "Balanced across subjects.")
        ),
    }


@api.get("/parent/digest/{child_id}")
async def parent_digest(child_id: str, user: dict = Depends(require_role('parent'))):
    parent = await db.users.find_one({"id": user["id"]})
    if child_id not in (parent.get("child_ids") or []):
        raise HTTPException(403, "Not your child")
    d = await build_child_digest(child_id)
    if not d:
        raise HTTPException(404, "Child not found")
    return d


def _cron_authorized(request: Request) -> bool:
    import hmac
    secret = os.environ.get("WEBHOOK_CRON_SECRET", "")
    auth = request.headers.get("Authorization", "")
    if not secret or not auth.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth.split(" ", 1)[1], secret)


async def _run_weekly_digests():
    """Background: email every parent a weekly digest for each linked child."""
    try:
        from email_utils import send_parent_digest_email
        parents = await db.users.find({"role": "parent"}, {"_id": 0}).to_list(2000)
        for p in parents:
            for cid in (p.get("child_ids") or []):
                try:
                    d = await build_child_digest(cid)
                    if d:
                        await send_parent_digest_email(p, d)
                except Exception as e:
                    log.warning("Digest for child %s failed: %s", cid, e)
    except Exception as e:
        log.error("Weekly digest run failed: %s", e)


@api.post("/cron/weekly-digest")
async def cron_weekly_digest(request: Request):
    # Cron endpoints must ack 2xx immediately; enqueue/background the actual work.
    if not _cron_authorized(request):
        raise HTTPException(401, "Unauthorized")
    asyncio.create_task(_run_weekly_digests())
    return {"ok": True, "status": "accepted"}
async def _run_study_reminders():
    """Background: nudge students (at their chosen IST hour) whose streak is at risk."""
    try:
        from email_utils import send_streak_reminder_email
        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        cur_hour = now_ist.hour
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        students = await db.users.find({"role": "student", "last_study_date": yesterday}, {"_id": 0}).to_list(5000)
        for s in students:
            if not s.get("email_results_enabled", True):
                continue
            if int(s.get("streak_days", 0) or 0) < 1:
                continue
            if int(s.get("reminder_hour", 19)) != cur_hour:
                continue
            await send_streak_reminder_email(s)
    except Exception as e:
        log.error("Study reminder run failed: %s", e)


@api.post("/cron/study-reminder")
async def cron_study_reminder(request: Request):
    # Cron endpoints must ack 2xx immediately; enqueue/background the actual work.
    if not _cron_authorized(request):
        raise HTTPException(401, "Unauthorized")
    asyncio.create_task(_run_study_reminders())
    return {"ok": True, "status": "accepted"}


@api.get("/meta/subjects")
async def subjects_meta():
    return {"subjects": ["Mathematics", "Physics", "Chemistry", "Biology"],
            "exam_types": ["full_mock", "chapter_wise", "topic_wise", "pyq", "parent_custom"],
            "difficulties": ["easy", "medium", "hard"],
            "question_types": ["mcq_single", "mcq_multi", "true_false", "integer", "assertion_reason", "match", "subjective", "image"]}


# ---------- DPP (Daily Practice Problems) ----------
@api.post("/dpp/generate")
async def dpp_generate(payload: dict, user: dict = Depends(require_role('student'))):
    """Generate today's DPP. Pick N questions across selected subjects/chapters."""
    subjects = payload.get("subjects") or ["Physics", "Chemistry", "Biology"]
    count = int(payload.get("count", 20))
    difficulty = payload.get("difficulty")
    today = datetime.now(timezone.utc).date().isoformat()
    existing = await db.dpps.find_one({"user_id": user["id"], "date": today}, {"_id": 0})
    if existing and not payload.get("regenerate"):
        return existing
    q: dict = {"status": "approved", "subject": {"$in": subjects}}
    if difficulty: q["difficulty"] = difficulty
    docs = await db.questions.find(q, {"_id": 0}).to_list(2000)
    random.shuffle(docs)
    picked = docs[:count]
    total = sum(x.get("marks", 4) for x in picked)
    t_id = new_id()
    t = {"id": t_id, "title": f"DPP · {today}", "exam_type": "dpp",
         "description": f"Daily Practice Problems — {count} Qs",
         "subjects": subjects, "duration_minutes": max(15, count * 1),
         "total_marks": total, "negative_marking": False,
         "shuffle_questions": False, "shuffle_options": False, "show_solutions_after": True,
         "question_ids": [d["id"] for d in picked], "sections": [],
         "assigned_to": [user["id"]], "created_by": user["id"], "created_by_role": "system",
         "created_at": now_iso()}
    await db.tests.insert_one(t)
    dpp = {"id": new_id(), "user_id": user["id"], "date": today,
           "test_id": t_id, "subjects": subjects, "count": count, "created_at": now_iso()}
    await db.dpps.update_one({"user_id": user["id"], "date": today}, {"$set": dpp}, upsert=True)
    dpp.pop("_id", None)
    return dpp


@api.get("/dpp/today")
async def dpp_today(user: dict = Depends(require_role('student'))):
    today = datetime.now(timezone.utc).date().isoformat()
    dpp = await db.dpps.find_one({"user_id": user["id"], "date": today}, {"_id": 0})
    if not dpp: return {"exists": False}
    dpp["exists"] = True
    return dpp


@api.get("/dpp/history")
async def dpp_history(user: dict = Depends(require_role('student'))):
    docs = await db.dpps.find({"user_id": user["id"]}, {"_id": 0}).sort("date", -1).limit(14).to_list(14)
    return docs


# ---------- Flashcards ----------
FLASHCARD_SEEDS = [
    {"subject": "Biology", "chapter": "Genetics", "front": "Mendel's Law of Segregation", "back": "Each pair of alleles segregates independently during gamete formation, so each gamete carries only one allele of a gene."},
    {"subject": "Biology", "chapter": "Genetics", "front": "Codon", "back": "A triplet of adjacent nucleotides in mRNA that codes for a specific amino acid. 64 possible codons, 20 amino acids + start/stop signals."},
    {"subject": "Biology", "chapter": "Human Physiology", "front": "Nephron", "back": "Structural and functional unit of the kidney. ~1 million per kidney. Filters blood, reabsorbs nutrients, secretes wastes."},
    {"subject": "Biology", "chapter": "Human Physiology", "front": "SA Node", "back": "Sinoatrial node — natural pacemaker of the heart, generates ~72 impulses/min in the right atrium wall."},
    {"subject": "Biology", "chapter": "Cell Biology", "front": "Mitochondria (NCERT line)", "back": "Sausage-shaped, double membrane, inner membrane forms cristae; house Krebs cycle & oxidative phosphorylation — ATP factory."},
    {"subject": "Biology", "chapter": "Ecology", "front": "Trophic level rule", "back": "Only ~10% of energy passes from one trophic level to the next (Lindeman's 10% law). Producers hold maximum energy."},
    {"subject": "Physics", "chapter": "Kinematics", "front": "Range of projectile", "back": "R = u² sin(2θ) / g. Maximum range when θ = 45°."},
    {"subject": "Physics", "chapter": "Optics", "front": "Lens formula", "back": "1/v − 1/u = 1/f. Sign convention: distances measured from optical centre; incident direction positive."},
    {"subject": "Physics", "chapter": "Electrostatics", "front": "Coulomb's law", "back": "F = k·q₁q₂/r². k = 9×10⁹ N·m²/C² in vacuum. Force is along the line joining charges."},
    {"subject": "Physics", "chapter": "Thermodynamics", "front": "First Law", "back": "ΔU = Q − W. Internal energy change equals heat absorbed minus work done by the system."},
    {"subject": "Chemistry", "chapter": "Atomic Structure", "front": "Aufbau Principle", "back": "Electrons fill orbitals in order of increasing energy: 1s < 2s < 2p < 3s < 3p < 4s < 3d …"},
    {"subject": "Chemistry", "chapter": "Chemical Bonding", "front": "VSEPR shapes", "back": "AB₂ linear, AB₃ trigonal planar, AB₄ tetrahedral, AB₅ trigonal bipyramidal, AB₆ octahedral."},
    {"subject": "Chemistry", "chapter": "Organic Chemistry", "front": "Markovnikov's Rule", "back": "In HX addition to unsymmetrical alkene, H adds to C with more H atoms; X adds to more substituted C."},
    {"subject": "Chemistry", "chapter": "Periodic Table", "front": "Electronegativity trend", "back": "Increases across a period (left→right), decreases down a group. Fluorine has the highest (3.98 Pauling)."},
    {"subject": "Chemistry", "chapter": "Electrochemistry", "front": "Nernst Equation", "back": "E = E° − (0.0591/n) log Q, at 298 K. Predicts cell EMF at non-standard conditions."},
    {"subject": "Biology", "chapter": "Morphology", "front": "Aestivation types", "back": "Valvate (petals touch), Twisted (one edge overlaps next), Imbricate (irregular), Vexillary (5 petals, standard largest — pea)."},
    {"subject": "Biology", "chapter": "Genetics", "front": "Hardy–Weinberg", "back": "p² + 2pq + q² = 1. Allele frequencies remain constant across generations if no evolution occurs."},
    {"subject": "Physics", "chapter": "Waves", "front": "Speed of sound in air", "back": "≈ 343 m/s at 20 °C. Depends on temperature: v ∝ √T (Kelvin)."},
    {"subject": "Physics", "chapter": "Electromagnetism", "front": "Lorentz force", "back": "F = q(E + v × B). Magnetic part is perpendicular to velocity, does no work."},
    {"subject": "Chemistry", "chapter": "Mole Concept", "front": "Molarity vs Molality", "back": "Molarity = mol solute / L solution (T-dependent). Molality = mol solute / kg solvent (T-independent)."},
]


@api.get("/flashcards")
async def list_flashcards(subject: Optional[str] = None, chapter: Optional[str] = None, user: dict = Depends(get_current_user)):
    # seed on first call
    if await db.flashcards.count_documents({}) == 0:
        for f in FLASHCARD_SEEDS:
            await db.flashcards.insert_one({**f, "id": new_id(), "created_at": now_iso()})
    q: dict = {}
    if subject: q["subject"] = subject
    if chapter: q["chapter"] = chapter
    return await db.flashcards.find(q, {"_id": 0}).limit(200).to_list(200)


@api.get("/flashcards/chapters")
async def flashcards_chapters(user: dict = Depends(get_current_user)):
    if await db.flashcards.count_documents({}) == 0:
        for f in FLASHCARD_SEEDS:
            await db.flashcards.insert_one({**f, "id": new_id(), "created_at": now_iso()})
    pipeline = [{"$group": {"_id": {"subject": "$subject", "chapter": "$chapter"}, "count": {"$sum": 1}}},
                {"$sort": {"count": -1}}]
    agg = await db.flashcards.aggregate(pipeline).to_list(200)
    return [{"subject": r["_id"]["subject"], "chapter": r["_id"]["chapter"], "cards": r["count"]} for r in agg]


# ---------- Mindmaps ----------
MINDMAP_SEEDS = [
    {"subject": "Biology", "chapter": "Genetics",
     "root": "Genetics",
     "branches": [
        {"name": "Mendel", "leaves": ["Segregation", "Independent Assortment", "Dominance"]},
        {"name": "DNA", "leaves": ["Watson-Crick", "Replication (semi-conservative)", "Transcription", "Translation"]},
        {"name": "Chromosomes", "leaves": ["Autosomes 22", "Sex XY", "Karyotype"]},
        {"name": "Mutation", "leaves": ["Point", "Frameshift", "Chromosomal"]},
     ]},
    {"subject": "Physics", "chapter": "Optics",
     "root": "Optics",
     "branches": [
        {"name": "Reflection", "leaves": ["Law · i=r", "Plane mirror", "Spherical mirror · 1/v+1/u=1/f"]},
        {"name": "Refraction", "leaves": ["Snell · n₁sinθ₁=n₂sinθ₂", "Lens · 1/v−1/u=1/f", "Prism · δ=(μ−1)A"]},
        {"name": "Wave optics", "leaves": ["Interference", "Diffraction", "Polarisation"]},
        {"name": "Instruments", "leaves": ["Microscope", "Telescope", "Human eye"]},
     ]},
    {"subject": "Chemistry", "chapter": "Electrochemistry",
     "root": "Electrochemistry",
     "branches": [
        {"name": "Cells", "leaves": ["Galvanic", "Electrolytic", "Daniel cell"]},
        {"name": "Laws", "leaves": ["Faraday I", "Faraday II", "Kohlrausch"]},
        {"name": "EMF", "leaves": ["Standard E°", "Nernst equation", "Salt bridge"]},
        {"name": "Applications", "leaves": ["Battery", "Corrosion", "Electroplating", "Fuel cell"]},
     ]},
    {"subject": "Biology", "chapter": "Human Physiology",
     "root": "Human Physiology",
     "branches": [
        {"name": "Digestion", "leaves": ["Mouth→Stomach→SI→LI", "Enzymes", "Absorption in villi"]},
        {"name": "Circulation", "leaves": ["4-chamber heart", "SA/AV node", "Systole/Diastole"]},
        {"name": "Respiration", "leaves": ["Alveoli", "O₂/CO₂ exchange", "Chloride shift"]},
        {"name": "Excretion", "leaves": ["Nephron", "GFR ~125 ml/min", "ADH regulation"]},
     ]},
    {"subject": "Physics", "chapter": "Kinematics",
     "root": "Kinematics",
     "branches": [
        {"name": "1D motion", "leaves": ["v = u+at", "s = ut+½at²", "v²=u²+2as"]},
        {"name": "Projectile", "leaves": ["R = u²sin2θ/g", "H = u²sin²θ/2g", "T = 2u sinθ/g"]},
        {"name": "Circular", "leaves": ["a = v²/r", "ω = v/r"]},
        {"name": "Relative", "leaves": ["v_ab = v_a − v_b"]},
     ]},
    {"subject": "Chemistry", "chapter": "Chemical Bonding",
     "root": "Chemical Bonding",
     "branches": [
        {"name": "Ionic", "leaves": ["Lattice energy", "Born-Haber", "Fajan's rules"]},
        {"name": "Covalent", "leaves": ["VSEPR shapes", "Hybridisation sp/sp²/sp³", "MO theory"]},
        {"name": "Coordinate", "leaves": ["Lone-pair donation", "Adducts"]},
        {"name": "Weak forces", "leaves": ["Hydrogen bond", "van der Waals", "Dipole-dipole"]},
     ]},
]


@api.get("/mindmaps")
async def list_mindmaps(subject: Optional[str] = None, user: dict = Depends(get_current_user)):
    if await db.mindmaps.count_documents({}) == 0:
        for m in MINDMAP_SEEDS:
            await db.mindmaps.insert_one({**m, "id": new_id(), "created_at": now_iso()})
    q = {"subject": subject} if subject else {}
    return await db.mindmaps.find(q, {"_id": 0}).to_list(200)


@api.get("/mindmaps/{mid}")
async def get_mindmap(mid: str, user: dict = Depends(get_current_user)):
    m = await db.mindmaps.find_one({"id": mid}, {"_id": 0})
    if not m: raise HTTPException(404, "Not found")
    return m


# ---------- Courses & Enrollment ----------
COURSE_SEEDS = [
    {"slug": "target-ascend-2027", "title": "Target Batch Ascend", "target": "NEET 2027", "kind": "1 Year CBT",
     "price": 799, "mrp": 1499, "featured": True, "rating": 4.9, "reviews": 1024, "enrolments": 2000,
     "highlights": ["Complete question bank", "Recorded video lectures", "DPP & micro-targets",
                    "10,000+ flashcards", "NCERT filter", "Performance snapshot"],
     "subjects": ["Physics", "Chemistry", "Biology"], "duration_months": 12},
    {"slug": "target-vital-2027", "title": "Target Batch Vital", "target": "NEET 2027", "kind": "Test Series CBT",
     "price": 499, "mrp": 999, "featured": False, "rating": 4.9, "reviews": 912, "enrolments": 2100,
     "highlights": ["High-yield classroom tests", "NTA-pattern simulation", "Full-syllabus + chapter tests",
                    "Smart analytics", "Detailed solutions", "Question bookmarking"],
     "subjects": ["Physics", "Chemistry", "Biology"], "duration_months": 12},
    {"slug": "masterclass-biology-2027", "title": "Masterclass in Biology", "target": "NEET 2027", "kind": "Biology CBT",
     "price": 399, "mrp": 899, "featured": False, "rating": 4.8, "reviews": 462, "enrolments": 810,
     "highlights": ["NCERT Companion · Class 11+12", "Biology Masterclass Lectures",
                    "Line-by-line NCERT coverage", "High-yield diagrams & mnemonics",
                    "Chapter-wise practice", "Concept clarity"],
     "subjects": ["Biology"], "duration_months": 12},
    {"slug": "target-aiims-2028", "title": "Target AIIMS Batch", "target": "NEET 2028", "kind": "2 Year CBT",
     "price": 1499, "mrp": 2999, "featured": True, "rating": 4.8, "reviews": 151, "enrolments": 450,
     "highlights": ["Complete question bank", "Recorded video lectures", "DPP & micro-targets",
                    "15,000+ questions", "NCERT filter", "Performance snapshot"],
     "subjects": ["Physics", "Chemistry", "Biology"], "duration_months": 24},
    {"slug": "ignite-2028", "title": "Ignite Batch", "target": "NEET 2028", "kind": "2 Year Foundation CBT",
     "price": 899, "mrp": 1799, "featured": False, "rating": 4.8, "reviews": 151, "enrolments": 437,
     "highlights": ["Complete question bank", "Recorded video lectures", "DPP & micro-targets",
                    "15,000+ questions", "NCERT filter", "Performance snapshot"],
     "subjects": ["Physics", "Chemistry", "Biology"], "duration_months": 24},
    {"slug": "jumbo-2028", "title": "Jumbo Test Series", "target": "NEET 2028", "kind": "Test Series CBT",
     "price": 599, "mrp": 1299, "featured": False, "rating": 4.8, "reviews": 15000, "enrolments": 12456,
     "highlights": ["Complete question bank", "Recorded video lectures", "DPP & micro-targets",
                    "15,000+ questions", "NCERT filter", "Performance snapshot"],
     "subjects": ["Physics", "Chemistry", "Biology"], "duration_months": 12},
]


@api.get("/courses")
async def list_courses(target: Optional[str] = None):
    if await db.courses.count_documents({}) == 0:
        for c in COURSE_SEEDS:
            await db.courses.insert_one({**c, "id": new_id(), "created_at": now_iso()})
    q = {"target": target} if target else {}
    docs = await db.courses.find(q, {"_id": 0}).to_list(50)
    return docs


@api.get("/courses/{slug}")
async def get_course(slug: str):
    if await db.courses.count_documents({}) == 0:
        for c in COURSE_SEEDS:
            await db.courses.insert_one({**c, "id": new_id(), "created_at": now_iso()})
    c = await db.courses.find_one({"slug": slug}, {"_id": 0})
    if not c: raise HTTPException(404, "Course not found")
    return c


class EnrollIn(BaseModel):
    course_slug: str
    payment_method: str = "test"


@api.post("/enroll")
async def enroll(inp: EnrollIn, user: dict = Depends(get_current_user)):
    course = await db.courses.find_one({"slug": inp.course_slug}, {"_id": 0})
    if not course: raise HTTPException(404, "Course not found")
    existing = await db.enrollments.find_one({"user_id": user["id"], "course_slug": inp.course_slug}, {"_id": 0})
    if existing: return existing
    e = {"id": new_id(), "user_id": user["id"], "course_slug": inp.course_slug,
         "course_title": course["title"], "target": course["target"],
         "amount_paid": course["price"], "payment_method": inp.payment_method,
         "status": "active", "enrolled_at": now_iso(),
         "expires_at": (datetime.now(timezone.utc) + timedelta(days=30 * course.get("duration_months", 12))).isoformat()}
    await db.enrollments.insert_one(e)
    await db.notifications.insert_one({"id": new_id(), "user_id": user["id"], "type": "enrollment",
        "message": f"You’re enrolled in {course['title']}!", "created_at": now_iso(), "read": False})
    e.pop("_id", None)
    return e


@api.get("/enrollments/me")
async def my_enrollments(user: dict = Depends(get_current_user)):
    return await db.enrollments.find({"user_id": user["id"]}, {"_id": 0}).sort("enrolled_at", -1).to_list(50)


# ---------- Counselling ----------
class CounsellingIn(BaseModel):
    name: str
    email: EmailStr
    phone: str
    student_class: str = "Class 12"
    target_year: str = "NEET 2027"
    message: str = ""


@api.post("/counselling")
async def create_counselling(inp: CounsellingIn):
    doc = inp.model_dump()
    doc.update({"id": new_id(), "created_at": now_iso(), "status": "new"})
    await db.counselling.insert_one(doc)
    doc.pop("_id", None)
    return {"ok": True, "id": doc["id"]}


@api.get("/counselling")
async def list_counselling(user: dict = Depends(require_role('admin'))):
    return await db.counselling.find({}, {"_id": 0}).sort("created_at", -1).limit(200).to_list(200)


# ---------- Rank Predictor ----------
@api.post("/rank-predictor")
async def rank_predictor(payload: dict, user: dict = Depends(get_current_user)):
    """Predict All India Rank from projected score. Uses simple bell-curve on cohort attempts."""
    score = float(payload.get("score", 0))
    total = float(payload.get("total", 720))
    target = payload.get("target", "NEET")
    pct = (score / max(total, 1)) * 100
    # neetprep-style band mapping
    if pct >= 92: rank_low, rank_high, band = 1, 500, "AIIMS Delhi / Top Govt"
    elif pct >= 85: rank_low, rank_high, band = 501, 3000, "Top Govt Medical College"
    elif pct >= 75: rank_low, rank_high, band = 3001, 12000, "Govt Medical College"
    elif pct >= 65: rank_low, rank_high, band = 12001, 40000, "State Govt / Deemed"
    elif pct >= 50: rank_low, rank_high, band = 40001, 120000, "Private / Management Quota"
    else: rank_low, rank_high, band = 120001, 500000, "Below cutoff · Push harder"
    cohort = await db.attempts.count_documents({"status": "submitted"})
    return {"score": score, "total": total, "percentage": round(pct, 2),
            "predicted_rank_low": rank_low, "predicted_rank_high": rank_high,
            "band": band, "target": target, "cohort_size": cohort,
            "message": f"Projected AIR {rank_low:,}–{rank_high:,}. Band: {band}."}


# ---------- Manual Grading (subjective + image_answer) ----------
@api.get("/grading/pending")
async def grading_pending(user: dict = Depends(require_role('admin'))):
    """Return every attempt that has at least one detailed answer marked pending_grading."""
    attempts = await db.attempts.find({"status": "submitted", "detailed.result": "pending_grading"},
                                     {"_id": 0}).sort("submitted_at", -1).to_list(200)
    q_ids = list({d.get("question_id") for a in attempts for d in a.get("detailed", []) if d.get("result") == "pending_grading"})
    u_ids = list({a["user_id"] for a in attempts})
    questions = {q["id"]: q for q in await db.questions.find({"id": {"$in": q_ids}}, {"_id": 0}).to_list(len(q_ids))}
    users = {u["id"]: u for u in await db.users.find({"id": {"$in": u_ids}}, {"_id": 0, "id": 1, "name": 1, "email": 1}).to_list(len(u_ids))}
    out = []
    for a in attempts:
        for d in a.get("detailed", []):
            if d.get("result") != "pending_grading": continue
            q = questions.get(d.get("question_id"), {})
            u = users.get(a["user_id"], {})
            out.append({
                "attempt_id": a["id"], "test_id": a["test_id"],
                "question_id": d["question_id"], "question_text": q.get("text", ""),
                "question_marks": q.get("marks", 4), "question_chapter": q.get("chapter", ""),
                "question_subject": q.get("subject", ""),
                "student_id": a["user_id"], "student_name": u.get("name", "Student"),
                "student_email": u.get("email", ""),
                "submitted_at": a.get("submitted_at"),
                "user_answer": d.get("user_answer", []),
                "image_answer": d.get("image_answer"),
                "marks_awarded": d.get("marks_awarded", 0),
                "grader_comment": d.get("grader_comment", ""),
            })
    return out


class GradeIn(BaseModel):
    marks: float
    comment: Optional[str] = ""


@api.post("/grading/{attempt_id}/{question_id}")
async def grade_answer(attempt_id: str, question_id: str, inp: GradeIn, user: dict = Depends(require_role('admin'))):
    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0})
    if not a: raise HTTPException(404, "Attempt not found")
    q = await db.questions.find_one({"id": question_id}, {"_id": 0})
    max_marks = q.get("marks", 4) if q else 4
    marks = max(0.0, min(float(inp.marks), float(max_marks)))
    detailed = a.get("detailed", [])
    old_awarded = 0.0
    updated = False
    for d in detailed:
        if d.get("question_id") == question_id:
            old_awarded = float(d.get("marks_awarded") or 0)
            d["marks_awarded"] = marks
            d["grader_comment"] = inp.comment or ""
            d["result"] = "graded" if marks > 0 else "wrong"
            d["graded_by"] = user["id"]
            d["graded_at"] = now_iso()
            updated = True
            break
    if not updated: raise HTTPException(404, "Question not in this attempt")
    delta = marks - old_awarded
    new_score = float(a.get("score", 0)) + delta
    await db.attempts.update_one({"id": attempt_id}, {"$set": {"detailed": detailed, "score": new_score}})
    await db.notifications.insert_one({"id": new_id(), "user_id": a["user_id"], "type": "graded",
        "message": f"Your subjective answer was graded: {marks:.1f}/{max_marks} marks.",
        "created_at": now_iso(), "read": False})
    return {"ok": True, "marks_awarded": marks, "new_total_score": new_score}


# ---------- Answer Key Uploader ----------
class AnswerKeyIn(BaseModel):
    key_text: str
    question_ids: Optional[List[str]] = None
    subject: Optional[str] = None
    chapter: Optional[str] = None
    overwrite: bool = False


@api.post("/questions/apply-answer-key")
async def apply_answer_key(inp: AnswerKeyIn, user: dict = Depends(require_role('admin'))):
    """Parse pasted answer-key text and assign correct answers to matching questions.
    Matches questions by order — the Nth question in the ordered set gets the Nth key entry.
    Ordering: (a) explicit question_ids list, else (b) newest-first by subject/chapter."""
    from regex_extractor import _extract_answer_key
    key_map = _extract_answer_key(inp.key_text)
    if not key_map: raise HTTPException(400, "Could not parse any answer-key entries from the pasted text.")

    if inp.question_ids:
        qs_docs = await db.questions.find({"id": {"$in": inp.question_ids}}, {"_id": 0}).to_list(len(inp.question_ids))
        ordered = sorted(qs_docs, key=lambda q: inp.question_ids.index(q["id"]))
    else:
        filt: dict = {}
        if inp.subject: filt["subject"] = inp.subject
        if inp.chapter: filt["chapter"] = inp.chapter
        ordered = await db.questions.find(filt, {"_id": 0}).sort("created_at", -1).limit(500).to_list(500)
        ordered.reverse()  # oldest-first so Q1 = first imported

    updated = 0; skipped = 0
    for i, q in enumerate(ordered, start=1):
        if i not in key_map:
            skipped += 1; continue
        if q.get("correct") and not inp.overwrite:
            skipped += 1; continue
        await db.questions.update_one({"id": q["id"]}, {"$set": {"correct": [key_map[i]]}})
        updated += 1
    return {"parsed_entries": len(key_map), "matched_questions": len(ordered),
            "updated": updated, "skipped": skipped, "map_preview": dict(list(key_map.items())[:10])}


# ---------- Chapter Playlist ----------
@api.get("/playlist/{subject}/{chapter}")
async def chapter_playlist(subject: str, chapter: str, user: dict = Depends(get_current_user)):
    """Return a bundled 25-min chapter revision playlist: DPP quick-set + flashcards + mindmap."""
    if await db.flashcards.count_documents({}) == 0:
        for f in FLASHCARD_SEEDS:
            await db.flashcards.insert_one({**f, "id": new_id(), "created_at": now_iso()})
    if await db.mindmaps.count_documents({}) == 0:
        for m in MINDMAP_SEEDS:
            await db.mindmaps.insert_one({**m, "id": new_id(), "created_at": now_iso()})
    dpp_qs = await db.questions.find(
        {"status": "approved", "subject": subject,
         "$or": [{"chapter": chapter}, {"chapter": {"$regex": chapter, "$options": "i"}}]},
        {"_id": 0}
    ).to_list(200)
    if len(dpp_qs) < 5:
        dpp_qs += await db.questions.find({"status": "approved", "subject": subject}, {"_id": 0}).limit(10).to_list(10)
    random.shuffle(dpp_qs); dpp_qs = dpp_qs[:10]
    flashcards = await db.flashcards.find({"subject": subject, "chapter": chapter}, {"_id": 0}).to_list(50)
    mindmap = await db.mindmaps.find_one({"subject": subject, "chapter": chapter}, {"_id": 0})
    total_minutes = 10 + max(2, len(flashcards) // 4) + 5  # dpp + flashcards + mindmap read
    return {"subject": subject, "chapter": chapter,
            "steps": [
                {"kind": "dpp", "title": "Warm-up · quick MCQs", "estimate_min": 10,
                 "questions_count": len(dpp_qs), "questions": dpp_qs},
                {"kind": "flashcards", "title": "Fast recall · flashcards", "estimate_min": max(2, len(flashcards) // 4),
                 "cards_count": len(flashcards), "cards": flashcards},
                {"kind": "mindmap", "title": "Connect the dots · mindmap", "estimate_min": 5,
                 "mindmap": mindmap},
            ],
            "total_minutes": total_minutes}


@api.get("/playlist/chapters")
async def playlist_chapters(user: dict = Depends(get_current_user)):
    """Chapters that have at least a mindmap OR flashcards — used for the playlist picker."""
    if await db.mindmaps.count_documents({}) == 0:
        for m in MINDMAP_SEEDS:
            await db.mindmaps.insert_one({**m, "id": new_id(), "created_at": now_iso()})
    docs = await db.mindmaps.find({}, {"_id": 0, "subject": 1, "chapter": 1}).to_list(200)
    seen = set(); out = []
    for d in docs:
        key = f"{d['subject']}|{d['chapter']}"
        if key in seen: continue
        seen.add(key); out.append(d)
    return out


# ---------- Referrals ----------
BONUS_DAYS_PER_REFERRAL = 7


@api.get("/referrals/me")
async def my_referrals(user: dict = Depends(get_current_user)):
    me = await db.users.find_one({"id": user["id"]}, {"_id": 0, "referral_code": 1, "name": 1})
    refs = await db.referrals.find({"referrer_id": user["id"]}, {"_id": 0}).to_list(200)
    return {
        "code": me.get("referral_code"),
        "invite_link": f"/signup?ref={me.get('referral_code')}",
        "referral_count": len(refs),
        "coins_earned": sum(r.get("coins_awarded", 0) for r in refs),
        "bonus_days_unlocked": len(refs) * BONUS_DAYS_PER_REFERRAL,
        "bonus_days_per_friend": BONUS_DAYS_PER_REFERRAL,
        "recent": refs[-5:],
    }


# ---------- Teacher / Vendor permissions & portal ----------
@api.get("/teachers")
async def list_teachers(user: dict = Depends(require_role('admin'))):
    docs = await db.users.find({"role": "teacher"}, {"_id": 0, "password": 0}).sort("created_at", -1).to_list(500)
    return docs

@api.post("/teachers/permissions")
async def set_teacher_perms(inp: TeacherPermIn, user: dict = Depends(require_role('admin'))):
    t = await db.users.find_one({"id": inp.teacher_id, "role": "teacher"})
    if not t: raise HTTPException(404, "Teacher not found")
    perms = {"exams": inp.exams, "subjects": inp.subjects, "classes": inp.classes,
             "can_print": inp.can_print, "can_view_results": inp.can_view_results}
    await db.users.update_one({"id": inp.teacher_id}, {"$set": {"teacher_perms": perms}})
    return {"ok": True, "teacher_perms": perms}

@api.get("/teacher/permissions")
async def my_teacher_perms(user: dict = Depends(require_role('teacher'))):
    return {"teacher_perms": user.get("teacher_perms") or {"exams": [], "subjects": [], "classes": [], "can_print": True, "can_view_results": True}}


# ---------- History-aware practice generation (no repeated questions) ----------
class PracticeIn(BaseModel):
    subjects: List[str] = []
    chapters: List[str] = []
    difficulty: Optional[str] = None
    count: int = 20
    duration_minutes: Optional[int] = None
    allow_repeat: bool = False

@api.post("/practice/generate")
async def practice_generate(inp: PracticeIn, user: dict = Depends(require_role('student'))):
    """Build a fresh practice paper for the student, excluding previously seen questions."""
    # 1) collect questions the student has already been served (from their attempts' tests)
    seen_qids: set = set()
    if not inp.allow_repeat:
        my_attempts = await db.attempts.find({"user_id": user["id"]}, {"_id": 0, "test_id": 1, "detailed": 1}).to_list(2000)
        seen_test_ids = list({a["test_id"] for a in my_attempts if a.get("test_id")})
        for d_list in (a.get("detailed") for a in my_attempts):
            for d in (d_list or []):
                if d.get("question_id"): seen_qids.add(d["question_id"])
        if seen_test_ids:
            seen_tests = await db.tests.find({"id": {"$in": seen_test_ids}}, {"_id": 0, "question_ids": 1}).to_list(2000)
            for st in seen_tests:
                seen_qids.update(st.get("question_ids", []))
    # 2) query the bank
    q: dict = {"status": "approved"}
    if inp.subjects: q["subject"] = {"$in": inp.subjects}
    if inp.chapters: q["chapter"] = {"$in": inp.chapters}
    if inp.difficulty: q["difficulty"] = inp.difficulty
    if seen_qids: q["id"] = {"$nin": list(seen_qids)}
    docs = await db.questions.find(q, {"_id": 0}).to_list(5000)
    exhausted = False
    if len(docs) < inp.count and not inp.allow_repeat:
        # pool exhausted → allow repeats to still deliver a paper
        exhausted = True
        q.pop("id", None)
        docs = await db.questions.find(q, {"_id": 0}).to_list(5000)
    random.shuffle(docs)
    picked = docs[:inp.count]
    if not picked:
        raise HTTPException(400, "No questions available for the selected filters. Ask admin to import more.")
    total = sum(x.get("marks", 4) for x in picked)
    tid = new_id()
    t = {"id": tid, "title": f"Practice · {(inp.subjects[0] if inp.subjects else 'Mixed')} · {len(picked)} Qs",
         "exam_type": "practice", "description": "Self-generated practice paper (history-aware).",
         "subjects": inp.subjects or [], "duration_minutes": inp.duration_minutes or max(10, len(picked)),
         "total_marks": total, "negative_marking": True, "shuffle_questions": True,
         "shuffle_options": False, "show_solutions_after": True,
         "question_ids": [d["id"] for d in picked], "sections": [],
         "assigned_to": [user["id"]], "created_by": user["id"], "created_by_role": "student",
         "created_at": now_iso()}
    await db.tests.insert_one(t)
    t.pop("_id", None)
    t["exhausted_pool"] = exhausted
    t["new_questions"] = len(picked)
    return t


# ---------- Question usage tracking ----------
@api.get("/questions/{qid}/usage")
async def question_usage(qid: str, user: dict = Depends(require_role('admin', 'teacher'))):
    q = await db.questions.find_one({"id": qid}, {"_id": 0})
    if not q: raise HTTPException(404, "Question not found")
    tests = await db.tests.find({"question_ids": qid}, {"_id": 0, "id": 1, "title": 1}).to_list(500)
    test_ids = [t["id"] for t in tests]
    attempts = await db.attempts.find({"test_id": {"$in": test_ids}, "status": "submitted"}, {"_id": 0, "detailed": 1}).to_list(5000)
    attempted = correct = wrong = 0
    for a in attempts:
        for d in (a.get("detailed") or []):
            if d.get("question_id") == qid:
                attempted += 1
                if d.get("result") == "correct": correct += 1
                elif d.get("result") == "wrong": wrong += 1
    accuracy = round(correct / attempted * 100, 1) if attempted else 0
    return {"question_id": qid, "used_in": tests, "used_in_count": len(tests),
            "attempted_by": attempted, "correct": correct, "wrong": wrong, "accuracy": accuracy}


# ---------- Test question-wise analysis (for teachers/admin) ----------
@api.get("/tests/{tid}/question-analysis")
async def test_question_analysis(tid: str, user: dict = Depends(require_role('admin', 'teacher'))):
    t = await db.tests.find_one({"id": tid}, {"_id": 0})
    if not t: raise HTTPException(404, "Test not found")
    if user["role"] == "teacher" and t.get("created_by") != user["id"]:
        raise HTTPException(403, "Not your test")
    attempts = await db.attempts.find({"test_id": tid, "status": "submitted"}, {"_id": 0}).to_list(5000)
    qs = await db.questions.find({"id": {"$in": t.get("question_ids", [])}}, {"_id": 0}).to_list(1000)
    qmap = {q["id"]: q for q in qs}
    stats: dict = {qid: {"correct": 0, "wrong": 0, "attempted": 0} for qid in t.get("question_ids", [])}
    scores = []
    for a in attempts:
        scores.append(a.get("score", 0))
        for d in (a.get("detailed") or []):
            qid = d.get("question_id")
            if qid in stats:
                if d.get("result") in ("correct", "wrong"):
                    stats[qid]["attempted"] += 1
                if d.get("result") == "correct": stats[qid]["correct"] += 1
                elif d.get("result") == "wrong": stats[qid]["wrong"] += 1
    rows = []
    for qid in t.get("question_ids", []):
        s = stats[qid]; q = qmap.get(qid, {})
        pct = round(s["correct"] / s["attempted"] * 100, 1) if s["attempted"] else 0
        rows.append({"question_id": qid, "text": (q.get("text") or "")[:160], "subject": q.get("subject", ""),
                     "difficulty": q.get("difficulty", ""), "correct_pct": pct,
                     "attempted": s["attempted"], "correct": s["correct"], "wrong": s["wrong"]})
    n = len(scores)
    return {"test": {"id": t["id"], "title": t.get("title"), "total_marks": t.get("total_marks", 0)},
            "students": n,
            "avg_score": round(sum(scores) / n, 2) if n else 0,
            "high_score": max(scores) if scores else 0,
            "low_score": min(scores) if scores else 0,
            "questions": rows}


# ---------- Live Video Classes (Zoom/Meet paste-link + keyless Jitsi) ----------
class LiveClassIn(BaseModel):
    title: str
    subject: Optional[str] = ""
    description: Optional[str] = ""
    provider: Literal['jitsi', 'zoom', 'meet', 'webinar.gg', 'custom'] = 'jitsi'
    meeting_url: Optional[str] = None
    starts_at: str                      # ISO datetime
    duration_minutes: int = 60
    assigned_to: List[str] = []         # student ids; empty = all students
    materials: List[dict] = []          # [{name, url}]
    recording_url: Optional[str] = None
    batch: Optional[str] = ""

class LiveClassUpdate(BaseModel):
    title: Optional[str] = None
    subject: Optional[str] = None
    description: Optional[str] = None
    meeting_url: Optional[str] = None
    starts_at: Optional[str] = None
    duration_minutes: Optional[int] = None
    materials: Optional[List[dict]] = None
    recording_url: Optional[str] = None
    assigned_to: Optional[List[str]] = None

class AttendanceMarkIn(BaseModel):
    student_id: str
    present: bool = True


def _class_status(c: dict, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    try:
        start = datetime.fromisoformat(c["starts_at"])
        if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
    except Exception:
        return "upcoming"
    end = start + timedelta(minutes=c.get("duration_minutes", 60))
    if now < start: return "upcoming"
    if now <= end: return "live"
    return "ended"


def _decorate_class(c: dict) -> dict:
    c = dict(c); c.pop("_id", None)
    c["status"] = _class_status(c)
    return c


@api.post("/live-classes")
async def create_live_class(inp: LiveClassIn, user: dict = Depends(require_role('admin', 'teacher'))):
    cid = new_id()
    url = (inp.meeting_url or "").strip()
    if inp.provider == "jitsi" and not url:
        room = re.sub(r'[^A-Za-z0-9]', '', (inp.title or "class").title())[:24] or "Class"
        url = f"https://meet.jit.si/MockTestClub-{room}-{cid[:8]}"
    if inp.provider in ("zoom", "meet", "webinar.gg", "custom") and not url:
        raise HTTPException(400, "A meeting link is required for this provider")
    doc = {
        "id": cid, "title": inp.title, "subject": inp.subject or "", "description": inp.description or "",
        "provider": inp.provider, "meeting_url": url,
        "starts_at": inp.starts_at, "duration_minutes": inp.duration_minutes,
        "assigned_to": inp.assigned_to, "materials": inp.materials or [],
        "recording_url": inp.recording_url, "batch": inp.batch or "",
        "host_id": user["id"], "host_name": user["name"], "host_role": user["role"],
        "created_at": now_iso(),
    }
    await db.live_classes.insert_one(doc)
    # reminders: notify assigned students (or all) + their parents
    if inp.assigned_to:
        students = await db.users.find({"id": {"$in": inp.assigned_to}, "role": "student"}, {"_id": 0}).to_list(2000)
    else:
        students = await db.users.find({"role": "student"}, {"_id": 0}).to_list(2000)
    notes = []
    parent_ids = set()
    for s in students:
        notes.append({"id": new_id(), "user_id": s["id"], "type": "live_class",
                      "message": f"Live class scheduled: {inp.title}", "class_id": cid,
                      "created_at": now_iso(), "read": False})
        for pid in (s.get("parent_ids") or []): parent_ids.add(pid)
    for pid in parent_ids:
        notes.append({"id": new_id(), "user_id": pid, "type": "live_class",
                      "message": f"Live class scheduled for your child: {inp.title}", "class_id": cid,
                      "created_at": now_iso(), "read": False})
    if notes: await db.notifications.insert_many(notes)
    return _decorate_class(doc)


@api.get("/live-classes")
async def list_live_classes(status_f: Optional[str] = None, user: dict = Depends(get_current_user)):
    if user["role"] == "admin":
        q: dict = {}
    elif user["role"] == "teacher":
        q = {"host_id": user["id"]}
    elif user["role"] == "student":
        q = {"$or": [{"assigned_to": user["id"]}, {"assigned_to": []}]}
    elif user["role"] == "parent":
        parent = await db.users.find_one({"id": user["id"]}, {"_id": 0, "child_ids": 1})
        kids = (parent or {}).get("child_ids") or []
        q = {"$or": [{"assigned_to": {"$in": kids}}, {"assigned_to": []}]}
    else:
        q = {}
    docs = await db.live_classes.find(q, {"_id": 0}).sort("starts_at", -1).to_list(500)
    out = [_decorate_class(c) for c in docs]
    if status_f: out = [c for c in out if c["status"] == status_f]
    return out


@api.get("/live-classes/live-now")
async def live_now(user: dict = Depends(get_current_user)):
    all_c = await list_live_classes(None, user)
    return [c for c in all_c if c["status"] == "live"]


@api.get("/live-classes/{cid}")
async def get_live_class(cid: str, user: dict = Depends(get_current_user)):
    c = await db.live_classes.find_one({"id": cid}, {"_id": 0})
    if not c: raise HTTPException(404, "Class not found")
    return _decorate_class(c)


@api.put("/live-classes/{cid}")
async def update_live_class(cid: str, inp: LiveClassUpdate, user: dict = Depends(require_role('admin', 'teacher'))):
    c = await db.live_classes.find_one({"id": cid}, {"_id": 0})
    if not c: raise HTTPException(404, "Class not found")
    if user["role"] == "teacher" and c.get("host_id") != user["id"]:
        raise HTTPException(403, "Not your class")
    upd = {k: v for k, v in inp.model_dump().items() if v is not None}
    upd["updated_at"] = now_iso()
    await db.live_classes.update_one({"id": cid}, {"$set": upd})
    return _decorate_class(await db.live_classes.find_one({"id": cid}, {"_id": 0}))


@api.delete("/live-classes/{cid}")
async def delete_live_class(cid: str, user: dict = Depends(require_role('admin', 'teacher'))):
    c = await db.live_classes.find_one({"id": cid}, {"_id": 0})
    if not c: raise HTTPException(404, "Class not found")
    if user["role"] == "teacher" and c.get("host_id") != user["id"]:
        raise HTTPException(403, "Not your class")
    await db.live_classes.delete_one({"id": cid})
    await db.live_attendance.delete_many({"class_id": cid})
    return {"ok": True}


@api.post("/live-classes/{cid}/join")
async def join_live_class(cid: str, user: dict = Depends(require_role('student'))):
    c = await db.live_classes.find_one({"id": cid}, {"_id": 0})
    if not c: raise HTTPException(404, "Class not found")
    assigned = c.get("assigned_to") or []
    if assigned and user["id"] not in assigned:
        raise HTTPException(403, "You are not assigned to this class")
    existing = await db.live_attendance.find_one({"class_id": cid, "student_id": user["id"]}, {"_id": 0})
    if not existing:
        await db.live_attendance.insert_one({
            "id": new_id(), "class_id": cid, "student_id": user["id"],
            "student_name": user["name"], "joined_at": now_iso(),
            "present": True, "marked_by": "self",
        })
    return {"meeting_url": c["meeting_url"], "provider": c["provider"], "title": c["title"]}


@api.get("/live-classes/{cid}/attendance")
async def class_attendance(cid: str, user: dict = Depends(require_role('admin', 'teacher'))):
    c = await db.live_classes.find_one({"id": cid}, {"_id": 0})
    if not c: raise HTTPException(404, "Class not found")
    if user["role"] == "teacher" and c.get("host_id") != user["id"]:
        raise HTTPException(403, "Not your class")
    assigned = c.get("assigned_to") or []
    if assigned:
        roster = await db.users.find({"id": {"$in": assigned}, "role": "student"}, {"_id": 0, "id": 1, "name": 1, "email": 1}).to_list(2000)
    else:
        roster = await db.users.find({"role": "student"}, {"_id": 0, "id": 1, "name": 1, "email": 1}).to_list(2000)
    att = {a["student_id"]: a for a in await db.live_attendance.find({"class_id": cid}, {"_id": 0}).to_list(5000)}
    rows = []
    for s in roster:
        a = att.get(s["id"])
        rows.append({"student_id": s["id"], "name": s["name"], "email": s.get("email", ""),
                     "present": bool(a and a.get("present")), "joined_at": a.get("joined_at") if a else None,
                     "marked_by": a.get("marked_by") if a else None})
    present = sum(1 for r in rows if r["present"])
    return {"class": _decorate_class(c), "total": len(rows), "present": present,
            "absent": len(rows) - present, "rows": rows}


@api.post("/live-classes/{cid}/attendance/mark")
async def mark_attendance(cid: str, inp: AttendanceMarkIn, user: dict = Depends(require_role('admin', 'teacher'))):
    c = await db.live_classes.find_one({"id": cid}, {"_id": 0})
    if not c: raise HTTPException(404, "Class not found")
    if user["role"] == "teacher" and c.get("host_id") != user["id"]:
        raise HTTPException(403, "Not your class")
    s = await db.users.find_one({"id": inp.student_id}, {"_id": 0, "name": 1})
    await db.live_attendance.update_one(
        {"class_id": cid, "student_id": inp.student_id},
        {"$set": {"present": inp.present, "marked_by": user["role"],
                  "student_name": (s or {}).get("name", ""),
                  "joined_at": now_iso() if inp.present else None},
         "$setOnInsert": {"id": new_id()}},
        upsert=True)
    return {"ok": True}


@api.get("/live-classes/{cid}/attendance.csv")
async def attendance_csv(cid: str, user: dict = Depends(require_role('admin', 'teacher'))):
    data = await class_attendance(cid, user)
    def _esc(v):
        s = str(v if v is not None else "")
        return '"' + s.replace('"', '""') + '"'
    lines = ["Name,Email,Present,Joined At,Marked By"]
    for r in data["rows"]:
        lines.append(",".join([_esc(r["name"]), _esc(r["email"]), "Yes" if r["present"] else "No",
                               _esc(r["joined_at"] or ""), _esc(r["marked_by"] or "")]))
    return PlainTextResponse("\n".join(lines), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="attendance-{cid[:8]}.csv"'})


DOUBT_SYSTEM = """You are an expert, patient NEET/JEE tutor for Physics, Chemistry, Biology and Mathematics.
Solve the student's doubt with a clear, step-by-step explanation a Class 11/12 student can follow.
Format your answer in this structure:
**Concept:** the key idea/formula involved (1-2 lines)
**Step-by-step:** numbered steps of the reasoning/derivation
**Answer:** the final answer, clearly highlighted
**Tip:** one quick exam tip or common mistake to avoid
Keep it concise and accurate. Use simple math notation. If the doubt is unclear, state your assumption and answer the most likely intended question."""


class DoubtIn(BaseModel):
    question: str
    subject: Optional[str] = None


COACH_SYSTEM = """You are an elite NEET/JEE performance coach. Given a student's mock-test result data, write a crisp, honest but encouraging diagnosis for a Class 11/12 aspirant.
Respond ONLY as strict compact JSON (no markdown, no code fences) with keys:
overview (string, 2 sentences),
conceptual_gaps (array of objects {topic, note}, max 4),
silly_mistakes (array of short strings, max 3),
time_management (string, 1-2 sentences),
action_plan (array of 3-4 short imperative strings).
Base every point on the data provided. If the student did well, still give sharp next steps."""


def _coach_payload(attempt: dict, qmap: dict) -> dict:
    subs: dict = {}
    sure_wrong = 0
    guess_right = 0
    for d in attempt.get("detailed", []):
        q = qmap.get(d.get("question_id"), {})
        s = q.get("subject", "General")
        ch = q.get("chapter") or q.get("topic") or ""
        rec = subs.setdefault(s, {"correct": 0, "wrong": 0, "unattempted": 0, "weak_chapters": {}})
        r = d.get("result")
        if r == "correct":
            rec["correct"] += 1
        elif r == "wrong":
            rec["wrong"] += 1
            if ch:
                rec["weak_chapters"][ch] = rec["weak_chapters"].get(ch, 0) + 1
        else:
            rec["unattempted"] += 1
        if d.get("confidence") == "sure" and r == "wrong":
            sure_wrong += 1
        if d.get("confidence") == "guess" and r == "correct":
            guess_right += 1
    return {
        "test": attempt.get("test_title", ""),
        "score": attempt.get("score"), "total_marks": attempt.get("total_marks"),
        "correct": attempt.get("correct"), "wrong": attempt.get("wrong"),
        "unattempted": attempt.get("unattempted"), "time_taken_seconds": attempt.get("time_taken_seconds"),
        "subjects": subs, "overconfident_wrong": sure_wrong, "lucky_guesses": guess_right,
    }


async def generate_coach(attempt: dict, qmap: dict, force: bool = False, lang: str = "en"):
    """Generate (and cache on the attempt) an LLM performance diagnosis. Returns dict or None."""
    import json
    cache_key = "ai_coach_hi" if lang == "hi" else "ai_coach"
    if attempt.get(cache_key) and not force:
        return attempt[cache_key]
    key, _ai_prov, _ai_model = resolve_ai()
    if not key:
        return None
    payload = _coach_payload(attempt, qmap)
    system_msg = COACH_SYSTEM + (" Write ALL text values in natural Hindi (Devanagari script); keep subject/technical terms and formulae clear." if lang == "hi" else "")
    try:
        txt = (await ai_complete(system_msg, "Result data JSON:\n" + json.dumps(payload))).strip()
        start, end = txt.find("{"), txt.rfind("}")
        coach = json.loads(txt[start:end + 1])
    except Exception as e:
        log.warning("coach gen failed: %s", e)
        return None
    await db.attempts.update_one({"id": attempt["id"]}, {"$set": {cache_key: coach}})
    return coach


@api.post("/ai/performance-coach/{attempt_id}")
async def performance_coach(attempt_id: str, lang: str = "en", user: dict = Depends(require_role('student'))):
    a = await db.attempts.find_one({"id": attempt_id, "user_id": user["id"], "status": "submitted"}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Result not found")
    t = await db.tests.find_one({"id": a["test_id"]}, {"_id": 0})
    qs = await db.questions.find({"id": {"$in": (t or {}).get("question_ids", [])}}, {"_id": 0}).to_list(1000)
    qmap = {q["id"]: q for q in qs}
    a["test_title"] = (t or {}).get("title", "")
    coach = await generate_coach(a, qmap, lang=lang)
    if not coach:
        raise HTTPException(502, "Coach unavailable right now. Please try again shortly.")
    return coach


PLAN_SYSTEM = """You are a NEET/JEE study planner. Given a student's weak areas from a mock test, output a focused, realistic 7-day recovery plan that uses the platform's content types.
Respond ONLY as strict compact JSON (no markdown): {summary: string, days: [{day: number, focus: string, tasks: [{type: "DPP"|"Flashcards"|"Mock"|"Revision"|"Concept", title: string, detail: string}]}]}.
Give exactly 7 days, 2-3 tasks each, concrete and tied to the weakest subjects/chapters. Keep titles short (max 6 words)."""


async def generate_plan(attempt: dict, qmap: dict, force: bool = False):
    import json
    if attempt.get("ai_plan") and not force:
        return attempt["ai_plan"]
    key, _ai_prov, _ai_model = resolve_ai()
    if not key:
        return None
    payload = _coach_payload(attempt, qmap)
    try:
        txt = (await ai_complete(PLAN_SYSTEM, "Weak-area result data:\n" + json.dumps(payload))).strip()
        s, e = txt.find("{"), txt.rfind("}")
        plan = json.loads(txt[s:e + 1])
    except Exception as ex:
        log.warning("plan gen failed: %s", ex)
        return None
    await db.attempts.update_one({"id": attempt["id"]}, {"$set": {"ai_plan": plan}})
    return plan


@api.post("/ai/study-plan/{attempt_id}")
async def study_plan(attempt_id: str, user: dict = Depends(require_role('student'))):
    a = await db.attempts.find_one({"id": attempt_id, "user_id": user["id"], "status": "submitted"}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Result not found")
    t = await db.tests.find_one({"id": a["test_id"]}, {"_id": 0})
    qs = await db.questions.find({"id": {"$in": (t or {}).get("question_ids", [])}}, {"_id": 0}).to_list(1000)
    qmap = {q["id"]: q for q in qs}
    a["test_title"] = (t or {}).get("title", "")
    plan = await generate_plan(a, qmap)
    if not plan:
        raise HTTPException(502, "Study plan unavailable right now. Please try again shortly.")
    return {**plan, "plan_progress": a.get("plan_progress") or {}}


class PlanToggleIn(BaseModel):
    key: str
    done: bool


@api.post("/ai/study-plan/{attempt_id}/toggle")
async def plan_toggle(attempt_id: str, inp: PlanToggleIn, user: dict = Depends(require_role('student'))):
    a = await db.attempts.find_one({"id": attempt_id, "user_id": user["id"]}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Result not found")
    prog = a.get("plan_progress") or {}
    if inp.done:
        prog[inp.key] = True
    else:
        prog.pop(inp.key, None)
    await db.attempts.update_one({"id": attempt_id}, {"$set": {"plan_progress": prog}})
    return {"plan_progress": prog}


EXPLAIN_SYSTEM = """You are an expert NEET/JEE content author. Given a question with options and the correct answer, produce a clear STEPWISE solution, common misconceptions, and 3 fresh variants (easy, medium, hard) testing the same concept.
Respond ONLY as strict compact JSON (no markdown): {explanation: string (use \\n between numbered steps), misconceptions: [string], variants: [{difficulty: "easy"|"medium"|"hard", text: string, options: [4 strings], correct: [one option letter like "A"]}]}.
Variants must be single-correct MCQs with exactly 4 options."""


@api.post("/ai/explain/{question_id}")
async def ai_explain(question_id: str, save: bool = False, user: dict = Depends(require_role('admin'))):
    import json
    q = await db.questions.find_one({"id": question_id}, {"_id": 0})
    if not q:
        raise HTTPException(404, "Question not found")
    if not q.get("ai_explanation"):
        key, _ai_prov, _ai_model = resolve_ai()
        if not key:
            raise HTTPException(502, "AI unavailable")
        payload = {k: q.get(k) for k in ("text", "options", "correct", "subject", "chapter", "type")}
        try:
            txt = (await ai_complete(EXPLAIN_SYSTEM, "Question JSON:\n" + json.dumps(payload))).strip()
            s, e = txt.find("{"), txt.rfind("}")
            data = json.loads(txt[s:e + 1])
        except Exception as ex:
            log.warning("explain gen failed: %s", ex)
            raise HTTPException(502, "AI could not generate an explanation. Try again.")
        await db.questions.update_one({"id": question_id}, {"$set": {"ai_explanation": data}})
        q["ai_explanation"] = data
    data = q["ai_explanation"]
    saved = 0
    if save:
        for v in data.get("variants", []):
            nq = {"id": new_id(), "type": "mcq_single", "text": v.get("text", ""),
                  "options": v.get("options", []), "correct": v.get("correct", []),
                  "difficulty": v.get("difficulty", "medium"), "subject": q.get("subject", "General"),
                  "chapter": q.get("chapter", ""), "topic": q.get("topic", ""), "tags": q.get("tags", []),
                  "marks": q.get("marks", 4), "explanation": "", "hint": "", "status": "approved",
                  "created_at": now_iso(), "source": "ai_variant", "parent_id": question_id}
            await db.questions.insert_one(nq)
            saved += 1
    return {"explanation": data.get("explanation"), "misconceptions": data.get("misconceptions", []),
            "variants": data.get("variants", []), "saved": saved}


@api.post("/ai/doubt-solve")
async def ai_doubt_solve(inp: DoubtIn, user: dict = Depends(require_role('student'))):
    await rate_limit(f"ai-doubt:{user['id']}", limit=15, window_seconds=60)
    q = (inp.question or "").strip()
    if len(q) < 5:
        raise HTTPException(400, "Please type your doubt (at least a few words).")
    key, _ai_prov, _ai_model = resolve_ai()
    if not key:
        raise HTTPException(503, "AI tutor is not configured.")
    try:
        prompt = f"Subject: {inp.subject or 'auto-detect'}\n\nStudent's doubt:\n{q}"
        solution = await ai_complete(DOUBT_SYSTEM, prompt)
    except Exception as e:
        log.exception("doubt-solve fail")
        raise HTTPException(502, "AI tutor could not answer right now. Please try again.")
    doc = {"id": new_id(), "user_id": user["id"], "question": q,
           "subject": inp.subject, "solution": solution, "created_at": now_iso()}
    await db.doubts.insert_one(doc)
    return {"id": doc["id"], "question": q, "subject": inp.subject,
            "solution": solution, "created_at": doc["created_at"]}


@api.get("/ai/doubt-history")
async def ai_doubt_history(user: dict = Depends(require_role('student'))):
    return await db.doubts.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).limit(50).to_list(50)


@api.post("/practice/from-document")
async def practice_from_document(
    file: Optional[UploadFile] = File(None),
    raw_text: Optional[str] = Form(None),
    subject_default: Optional[str] = Form("Physics"),
    title: Optional[str] = Form(None),
    num_questions: Optional[int] = Form(15),
    user: dict = Depends(require_role('student')),
):
    """Turn a student's own notes/PDF into a fresh personal MCQ quiz they can attempt."""
    await rate_limit(f"pdfquiz:{user['id']}", limit=6, window_seconds=120)
    text = ""
    src_name = "your notes"
    if raw_text and raw_text.strip():
        text = raw_text.strip()
    elif file:
        data = await file.read()
        src_name = file.filename or "upload"
        ext = src_name.split(".")[-1].lower()
        if ext == "pdf":
            text = parse_pdf(data)
        elif ext == "docx":
            text = parse_docx(data)
        else:
            try: text = data.decode("utf-8", errors="ignore")
            except Exception: text = ""
    else:
        raise HTTPException(400, "Upload a PDF/DOCX/TXT file or paste your notes.")

    if not text or len(text.strip()) < 40:
        raise HTTPException(400, "Couldn't read enough text. Try a text-based PDF (not a scan) or paste your notes directly.")

    generated, errs = await ai_generate_quiz(text, subject_default or "Physics", num_questions or 15)
    if not generated:
        raise HTTPException(502, (errs[0] if errs else "AI could not create a quiz from this material."))

    now = now_iso()
    qids = []
    for g in generated:
        qid = new_id()
        g.update({"id": qid, "created_at": now, "created_by": user["id"],
                  "personal": True, "owner_id": user["id"], "status": "personal"})
        qids.append(qid)
    await db.questions.insert_many(generated)

    total = sum(int(g.get("marks", 4)) for g in generated)
    test = {"id": new_id(), "title": (title or f"Quiz from {src_name}")[:80],
            "exam_type": "personal", "description": "AI-generated from your material",
            "subjects": list({g.get("subject", subject_default) for g in generated}),
            "duration_minutes": max(5, len(qids) * 1),
            "total_marks": total, "negative_marking": False, "shuffle_questions": True,
            "shuffle_options": False, "show_solutions_after": True,
            "question_ids": qids, "sections": [], "assigned_to": [user["id"]],
            "created_by": user["id"], "created_by_role": "student",
            "personal": True, "owner_id": user["id"], "created_at": now}
    await db.tests.insert_one(test)
    return {"test_id": test["id"], "title": test["title"], "count": len(qids),
            "subjects": test["subjects"], "total_marks": total}


@api.get("/practice/my-quizzes")
async def my_personal_quizzes(user: dict = Depends(require_role('student'))):
    docs = await db.tests.find({"personal": True, "owner_id": user["id"]},
                               {"_id": 0}).sort("created_at", -1).limit(50).to_list(50)
    return [{"id": d["id"], "title": d["title"], "count": len(d.get("question_ids", [])),
             "subjects": d.get("subjects", []), "created_at": d.get("created_at")} for d in docs]


@api.get("/analytics/question-heatmap")
async def question_heatmap(subject: Optional[str] = None, user: dict = Depends(require_role('admin'))):
    """Per-question difficulty heatmap across ALL submitted attempts.

    Aggregates every answered question over all attempts and returns, per
    question: times seen, correct/wrong counts, accuracy %, and an
    observed-difficulty band derived from real student performance (which can
    differ from the author-tagged difficulty)."""
    attempts = await db.attempts.find({"status": "submitted"}, {"_id": 0, "detailed": 1}).to_list(5000)
    stats: dict = {}
    for a in attempts:
        for d in (a.get("detailed") or []):
            qid = d.get("question_id")
            if not qid:
                continue
            s = stats.setdefault(qid, {"seen": 0, "correct": 0, "wrong": 0, "unattempted": 0})
            s["seen"] += 1
            r = d.get("result")
            if r == "correct": s["correct"] += 1
            elif r == "wrong": s["wrong"] += 1
            else: s["unattempted"] += 1
    if not stats:
        return {"subjects": [], "cells": [], "total_questions": 0}
    qids = list(stats.keys())
    qfilter = {"id": {"$in": qids}, "personal": {"$ne": True}}
    if subject:
        qfilter["subject"] = subject
    qs = await db.questions.find(qfilter, {"_id": 0}).to_list(5000)
    cells = []
    subjects = set()
    for q in qs:
        s = stats.get(q["id"])
        if not s:
            continue
        answered = s["correct"] + s["wrong"]
        acc = round((s["correct"] / answered) * 100, 1) if answered else 0.0
        if answered == 0:
            band = "unknown"
        elif acc >= 75:
            band = "easy"
        elif acc >= 45:
            band = "medium"
        else:
            band = "hard"
        subjects.add(q.get("subject", "Other"))
        cells.append({
            "question_id": q["id"],
            "text": (q.get("text") or "")[:140],
            "subject": q.get("subject", "Other"),
            "chapter": q.get("chapter", ""),
            "topic": q.get("topic", ""),
            "tagged_difficulty": q.get("difficulty", "medium"),
            "seen": s["seen"], "correct": s["correct"], "wrong": s["wrong"],
            "unattempted": s["unattempted"],
            "accuracy": acc, "observed_difficulty": band,
        })
    cells.sort(key=lambda c: (c["subject"], c["accuracy"]))
    return {"subjects": sorted(subjects), "cells": cells, "total_questions": len(cells)}


@api.get("/leaderboard/live")
async def live_leaderboard(test_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    """Live class leaderboard. If test_id is given, ranks students by their best
    score on that test; otherwise ranks by total reward coins. Meant to be polled
    every few seconds so it reflects submissions in near real-time."""
    if test_id:
        attempts = await db.attempts.find(
            {"test_id": test_id, "status": "submitted"}, {"_id": 0}).to_list(2000)
        best: dict = {}
        for a in attempts:
            uid = a["user_id"]
            if uid not in best or a.get("score", 0) > best[uid]["score"]:
                best[uid] = {"score": a.get("score", 0), "correct": a.get("correct", 0),
                             "wrong": a.get("wrong", 0), "submitted_at": a.get("submitted_at"),
                             "time_taken_seconds": a.get("time_taken_seconds")}
        uids = list(best.keys())
        users = await db.users.find({"id": {"$in": uids}}, {"_id": 0, "password": 0}).to_list(2000)
        umap = {u["id"]: u for u in users}
        rows = [{"user": {"id": uid, "name": umap.get(uid, {}).get("name", "Student"),
                          "avatar": umap.get(uid, {}).get("avatar")},
                 **v} for uid, v in best.items() if uid in umap]
        rows.sort(key=lambda r: (-r["score"], r.get("time_taken_seconds") or 1e9))
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        t = await db.tests.find_one({"id": test_id}, {"_id": 0, "title": 1, "total_marks": 1})
        return {"kind": "test", "test": t or {}, "updated_at": now_iso(),
                "you": user["id"], "rows": rows[:100]}
    docs = await db.users.find({"role": "student"}, {"_id": 0, "password": 0}).sort("reward_coins", -1).limit(50).to_list(50)
    def pin(u):
        name = u.get("pinned_badge")
        if not name: return None, None
        icon = next((b.get("icon") for b in (u.get("earned_badges") or []) if b.get("name") == name), "sparkles")
        return name, icon
    rows = []
    for i, u in enumerate(docs):
        pname, picon = pin(u)
        rows.append({"rank": i + 1, "user": {"id": u["id"], "name": u["name"], "avatar": u.get("avatar"),
                                              "exam_target": u.get("exam_target"),
                                              "pinned_badge": pname, "pinned_badge_icon": picon},
                     "score": u.get("reward_coins", 0), "streak": u.get("streak_days", 0)})
    return {"kind": "coins", "updated_at": now_iso(), "you": user["id"], "rows": rows}


@api.get("/admin/errors")
async def list_errors(limit: int = 100, user: dict = Depends(require_role('admin'))):
    """Sentry-style error feed: recent captured backend exceptions."""
    docs = await db.error_logs.find({}, {"_id": 0}).sort("ts", -1).limit(min(limit, 200)).to_list(200)
    total = await db.error_logs.count_documents({})
    last_24h = await db.error_logs.count_documents(
        {"ts": {"$gte": (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()}})
    return {"total": total, "last_24h": last_24h, "errors": docs}


@api.post("/admin/errors/clear")
async def clear_errors(user: dict = Depends(require_role('admin'))):
    res = await db.error_logs.delete_many({})
    return {"cleared": res.deleted_count}


from math import floor as _floor


def _expand_answers(values, options):
    letters = "abcdefghij"
    out = set()
    opts = [str(o).strip().lower() for o in (options or [])]
    for v in (values or []):
        s = str(v).strip().lower()
        if not s:
            continue
        out.add(s)
        if len(s) == 1 and s in letters:
            i = letters.index(s)
            if i < len(opts):
                out.add(opts[i])
        elif s in opts:
            out.add(letters[opts.index(s)])
    return out


def _letters_only(st):
    letters = "abcdefghij"
    return {x for x in st if len(x) == 1 and x in letters}


def answer_is_correct(q, selected):
    corr = _letters_only(_expand_answers(q.get("correct"), q.get("options")))
    sel = _letters_only(_expand_answers(selected, q.get("options")))
    return bool(corr) and sel == corr


def _correct_letter(q):
    corr = _letters_only(_expand_answers(q.get("correct"), q.get("options")))
    return sorted(corr)


def _strip_q(q):
    """Client-safe question (no answer key)."""
    return {"id": q["id"], "text": q.get("text", ""), "type": q.get("type", "mcq_single"),
            "options": q.get("options", []), "subject": q.get("subject", ""),
            "chapter": q.get("chapter", ""), "topic": q.get("topic", ""),
            "difficulty": q.get("difficulty", "medium"), "marks": q.get("marks", 4),
            "image_url": q.get("image_url"), "image_alt": q.get("image_alt", "Question diagram")}


# ============================================================
# SPACED REPETITION (SM-2 lite) — resurface wrong/weak questions
# ============================================================
_SRS_INTERVALS = [1, 3, 7, 16, 35]  # days by rep level


async def sync_reviews_from_attempt(user_id: str, detailed: list, qmap: dict):
    """After a submission, schedule wrong questions for review and advance
    already-scheduled ones that were answered correctly."""
    now = datetime.now(timezone.utc)
    for d in (detailed or []):
        qid = d.get("question_id")
        q = qmap.get(qid)
        if not q or q.get("personal"):
            continue
        res = d.get("result")
        if res not in ("correct", "wrong"):
            continue
        rev = await db.reviews.find_one({"user_id": user_id, "question_id": qid})
        if res == "wrong":
            due = (now + timedelta(days=1)).isoformat()
            if rev:
                await db.reviews.update_one({"_id": rev["_id"]}, {"$set": {
                    "rep": 0, "due_at": due, "last_result": "wrong", "updated_at": now.isoformat()},
                    "$inc": {"lapses": 1}})
            else:
                await db.reviews.insert_one({"id": new_id(), "user_id": user_id, "question_id": qid,
                    "subject": q.get("subject", ""), "chapter": q.get("chapter", ""),
                    "rep": 0, "lapses": 1, "due_at": due, "last_result": "wrong",
                    "created_at": now.isoformat(), "updated_at": now.isoformat()})
        elif res == "correct" and rev:
            rep = min(rev.get("rep", 0) + 1, len(_SRS_INTERVALS) - 1)
            due = (now + timedelta(days=_SRS_INTERVALS[rep])).isoformat()
            await db.reviews.update_one({"_id": rev["_id"]}, {"$set": {
                "rep": rep, "due_at": due, "last_result": "correct", "updated_at": now.isoformat()}})


@api.get("/reviews/stats")
async def reviews_stats(user: dict = Depends(require_role('student'))):
    now_i = now_iso()
    total = await db.reviews.count_documents({"user_id": user["id"]})
    due = await db.reviews.count_documents({"user_id": user["id"], "due_at": {"$lte": now_i}})
    mastered = await db.reviews.count_documents({"user_id": user["id"], "rep": {"$gte": len(_SRS_INTERVALS) - 1}})
    return {"total": total, "due": due, "mastered": mastered, "learning": max(total - mastered, 0)}


@api.get("/reviews/due")
async def reviews_due(limit: int = 20, user: dict = Depends(require_role('student'))):
    now_i = now_iso()
    revs = await db.reviews.find({"user_id": user["id"], "due_at": {"$lte": now_i}},
                                 {"_id": 0}).sort("due_at", 1).limit(min(limit, 40)).to_list(40)
    qids = [r["question_id"] for r in revs]
    qs = await db.questions.find({"id": {"$in": qids}}, {"_id": 0}).to_list(60)
    qmap = {q["id"]: q for q in qs}
    out = []
    for r in revs:
        q = qmap.get(r["question_id"])
        if q:
            item = _strip_q(q)
            item["rep"] = r.get("rep", 0)
            out.append(item)
    return {"due": len(out), "questions": out}


class ReviewGradeIn(BaseModel):
    question_id: str
    selected: List[str] = []


@api.post("/reviews/grade")
async def reviews_grade(inp: ReviewGradeIn, user: dict = Depends(require_role('student'))):
    q = await db.questions.find_one({"id": inp.question_id}, {"_id": 0})
    if not q:
        raise HTTPException(404, "Question not found")
    correct = answer_is_correct(q, inp.selected)
    now = datetime.now(timezone.utc)
    rev = await db.reviews.find_one({"user_id": user["id"], "question_id": inp.question_id})
    if correct:
        rep = min((rev.get("rep", 0) if rev else 0) + 1, len(_SRS_INTERVALS) - 1)
        due = (now + timedelta(days=_SRS_INTERVALS[rep])).isoformat()
        upd = {"rep": rep, "due_at": due, "last_result": "correct", "updated_at": now.isoformat()}
    else:
        due = (now + timedelta(days=1)).isoformat()
        upd = {"rep": 0, "due_at": due, "last_result": "wrong", "updated_at": now.isoformat()}
    if rev:
        await db.reviews.update_one({"_id": rev["_id"]}, {"$set": upd, **({"$inc": {"lapses": 1}} if not correct else {})})
    else:
        await db.reviews.insert_one({"id": new_id(), "user_id": user["id"], "question_id": inp.question_id,
            "subject": q.get("subject", ""), "chapter": q.get("chapter", ""), "lapses": 0 if correct else 1,
            "created_at": now.isoformat(), **upd})
    return {"correct": correct, "correct_answer": _correct_letter(q),
            "correct_options": q.get("correct", []), "explanation": q.get("explanation", ""),
            "explanation_image_url": q.get("explanation_image_url")}


# ============================================================
# AI WEAKNESS COACH — daily "fix these 5 chapters" plan
# ============================================================
@api.get("/coach/plan")
async def coach_plan(user: dict = Depends(require_role('student'))):
    attempts = await db.attempts.find({"user_id": user["id"], "status": "submitted"},
                                      {"_id": 0, "detailed": 1}).to_list(500)
    chap: dict = {}
    for a in attempts:
        for d in (a.get("detailed") or []):
            qid = d.get("question_id")
            if not qid:
                continue
            res = d.get("result")
            key = qid
            # we need chapter/subject: fetch lazily below
            chap.setdefault(key, res)
    # Build chapter stats by joining questions
    qids = list(chap.keys())
    accuracy: dict = {}
    if qids:
        qs = await db.questions.find({"id": {"$in": qids}}, {"_id": 0, "id": 1, "subject": 1, "chapter": 1}).to_list(5000)
        qinfo = {q["id"]: q for q in qs}
        agg: dict = {}
        for a in attempts:
            for d in (a.get("detailed") or []):
                q = qinfo.get(d.get("question_id"))
                if not q or not q.get("chapter"):
                    continue
                k = (q.get("subject", "Other"), q.get("chapter"))
                s = agg.setdefault(k, {"correct": 0, "wrong": 0, "seen": 0})
                s["seen"] += 1
                if d.get("result") == "correct":
                    s["correct"] += 1
                elif d.get("result") == "wrong":
                    s["wrong"] += 1
        for (sub, ch), s in agg.items():
            answered = s["correct"] + s["wrong"]
            if answered < 2:
                continue
            acc = round(s["correct"] / answered * 100, 1)
            accuracy[(sub, ch)] = {"subject": sub, "chapter": ch, "accuracy": acc,
                                   "seen": s["seen"], "correct": s["correct"], "wrong": s["wrong"]}
    weak = sorted(accuracy.values(), key=lambda x: (x["accuracy"], -x["wrong"]))[:5]
    for w in weak:
        a = w["accuracy"]
        w["band"] = "critical" if a < 40 else ("weak" if a < 60 else "improving")
        w["action"] = f"Do a 10-question {w['chapter']} drill and review each solution."
    overall = round(sum(v["accuracy"] for v in accuracy.values()) / len(accuracy), 1) if accuracy else None
    return {"generated_at": now_iso(), "overall_accuracy": overall,
            "chapters_analysed": len(accuracy), "weak_chapters": weak,
            "message": ("Great work — not enough data yet. Attempt a few tests and your plan will appear here."
                        if not weak else f"Focus on these {len(weak)} chapters today to move your rank the fastest.")}


class DrillIn(BaseModel):
    subject: str
    chapter: str
    count: Optional[int] = 10


@api.post("/coach/drill")
async def coach_drill(inp: DrillIn, user: dict = Depends(require_role('student'))):
    q = {"subject": inp.subject, "chapter": inp.chapter, "personal": {"$ne": True}}
    docs = await db.questions.find(q, {"_id": 0}).to_list(500)
    if not docs:
        raise HTTPException(404, "No questions available for this chapter yet.")
    random.shuffle(docs)
    picked = docs[:max(3, min(int(inp.count or 10), 20))]
    qids = [d["id"] for d in picked]
    total = sum(int(d.get("marks", 4)) for d in picked)
    now = now_iso()
    test = {"id": new_id(), "title": f"{inp.chapter} — Weakness Drill", "exam_type": "drill",
            "description": f"Coach drill on {inp.chapter}", "subjects": [inp.subject],
            "duration_minutes": max(5, len(qids)), "total_marks": total,
            "negative_marking": False, "shuffle_questions": True, "shuffle_options": False,
            "show_solutions_after": True, "question_ids": qids, "sections": [],
            "assigned_to": [user["id"]], "created_by": user["id"], "created_by_role": "student",
            "personal": True, "owner_id": user["id"], "created_at": now}
    await db.tests.insert_one(test)
    return {"test_id": test["id"], "title": test["title"], "count": len(qids)}


# ============================================================
# ADAPTIVE (CAT-style) — difficulty rises/falls with performance
# ============================================================
_ADAPT_ORDER = ["easy", "medium", "hard"]


async def _pick_adaptive_q(subject, difficulty, asked):
    for diff in ([difficulty] + [d for d in _ADAPT_ORDER if d != difficulty]):
        docs = await db.questions.find(
            {"subject": subject, "difficulty": diff, "type": "mcq_single",
             "personal": {"$ne": True}, "id": {"$nin": asked}}, {"_id": 0}).to_list(200)
        if docs:
            return random.choice(docs)
    docs = await db.questions.find(
        {"subject": subject, "personal": {"$ne": True}, "id": {"$nin": asked}}, {"_id": 0}).to_list(200)
    return random.choice(docs) if docs else None


class AdaptiveStartIn(BaseModel):
    subject: str
    length: Optional[int] = 12


@api.post("/adaptive/start")
async def adaptive_start(inp: AdaptiveStartIn, user: dict = Depends(require_role('student'))):
    q = await _pick_adaptive_q(inp.subject, "medium", [])
    if not q:
        raise HTTPException(404, "No questions available for this subject yet.")
    sess = {"id": new_id(), "user_id": user["id"], "subject": inp.subject,
            "length": max(5, min(int(inp.length or 12), 25)), "difficulty": "medium",
            "asked": [q["id"]], "answered": 0, "correct": 0, "score_points": 0,
            "history": [], "status": "active", "created_at": now_iso()}
    await db.adaptive_sessions.insert_one(sess)
    return {"session_id": sess["id"], "question": _strip_q(q), "index": 1,
            "length": sess["length"], "difficulty": sess["difficulty"]}


class AdaptiveAnswerIn(BaseModel):
    session_id: str
    question_id: str
    selected: List[str] = []


@api.post("/adaptive/answer")
async def adaptive_answer(inp: AdaptiveAnswerIn, user: dict = Depends(require_role('student'))):
    s = await db.adaptive_sessions.find_one({"id": inp.session_id, "user_id": user["id"]}, {"_id": 0})
    if not s:
        raise HTTPException(404, "Session not found")
    if s["status"] != "active":
        raise HTTPException(400, "Session already finished")
    q = await db.questions.find_one({"id": inp.question_id}, {"_id": 0})
    if not q:
        raise HTTPException(404, "Question not found")
    correct = answer_is_correct(q, inp.selected)
    diff = s["difficulty"]
    di = _ADAPT_ORDER.index(diff)
    pts = {"easy": 1, "medium": 2, "hard": 3}[diff]
    new_di = min(di + 1, 2) if correct else max(di - 1, 0)
    new_diff = _ADAPT_ORDER[new_di]
    answered = s["answered"] + 1
    ncorrect = s["correct"] + (1 if correct else 0)
    score_points = s["score_points"] + (pts if correct else 0)
    hist = s.get("history", []) + [{"question_id": q["id"], "difficulty": diff, "correct": correct}]
    finished = answered >= s["length"]
    nextq = None
    if not finished:
        nextq = await _pick_adaptive_q(s["subject"], new_diff, s["asked"])
        finished = nextq is None
    asked = s["asked"] + ([nextq["id"]] if nextq else [])
    upd = {"difficulty": new_diff, "answered": answered, "correct": ncorrect,
           "score_points": score_points, "history": hist, "asked": asked,
           "status": "finished" if finished else "active"}
    await db.adaptive_sessions.update_one({"id": s["id"]}, {"$set": upd})
    resp = {"correct": correct, "correct_answer": _correct_letter(q),
            "explanation": q.get("explanation", ""), "explanation_image_url": q.get("explanation_image_url"), "next_difficulty": new_diff,
            "answered": answered, "length": s["length"], "correct_count": ncorrect,
            "finished": finished}
    if finished:
        acc = round(ncorrect / answered * 100, 1) if answered else 0
        # ability estimate: reward getting hard ones right
        max_pts = sum({"easy": 1, "medium": 2, "hard": 3}[h["difficulty"]] for h in hist) or 1
        ability = round(score_points / max_pts * 100, 1)
        band = "Advanced" if ability >= 75 else ("Proficient" if ability >= 50 else "Developing")
        resp["summary"] = {"accuracy": acc, "ability": ability, "band": band,
                           "peak_difficulty": _ADAPT_ORDER[max((_ADAPT_ORDER.index(h["difficulty"]) for h in hist), default=1)],
                           "correct": ncorrect, "total": answered}
    else:
        resp["question"] = _strip_q(nextq)
        resp["index"] = answered + 1
    return resp


# ============================================================
# QUIZ BATTLES — real-time multiplayer rooms (poll-based)
# ============================================================
_BATTLE_PER_Q_SECONDS = 20


def _battle_public(room, uid):
    """Compute live room state from the authoritative started_at clock."""
    now = datetime.now(timezone.utc)
    n = len(room.get("question_ids", []))
    perq = room.get("per_q_seconds", _BATTLE_PER_Q_SECONDS)
    state = {"id": room["id"], "code": room["code"], "subject": room.get("subject"),
             "status": room["status"], "host_id": room["host_id"], "num_questions": n,
             "per_q_seconds": perq, "you": uid}
    players = [{"user_id": p["user_id"], "name": p["name"], "score": p.get("score", 0),
                "answered": len(p.get("answers", []))} for p in room.get("players", [])]
    players.sort(key=lambda x: -x["score"])
    for i, p in enumerate(players):
        p["rank"] = i + 1
    state["players"] = players
    if room["status"] == "active" and room.get("started_at"):
        started = datetime.fromisoformat(room["started_at"])
        elapsed = (now - started).total_seconds()
        idx = int(elapsed // perq)
        if idx >= n:
            state["status"] = "finished"
        else:
            state["index"] = idx
            state["time_left"] = max(0, int(perq - (elapsed - idx * perq)))
            qid = room["question_ids"][idx]
            state["_current_qid"] = qid
    return state


class BattleCreateIn(BaseModel):
    subject: str
    num_questions: Optional[int] = 5


@api.post("/battles/create")
async def battle_create(inp: BattleCreateIn, user: dict = Depends(require_role('student'))):
    docs = await db.questions.find(
        {"subject": inp.subject, "type": "mcq_single", "personal": {"$ne": True}}, {"_id": 0}).to_list(300)
    if len(docs) < 3:
        raise HTTPException(404, "Not enough questions for this subject yet.")
    random.shuffle(docs)
    picked = docs[:max(3, min(int(inp.num_questions or 5), 15))]
    code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=5))
    room = {"id": new_id(), "code": code, "subject": inp.subject, "status": "lobby",
            "host_id": user["id"], "question_ids": [q["id"] for q in picked],
            "per_q_seconds": _BATTLE_PER_Q_SECONDS,
            "players": [{"user_id": user["id"], "name": user["name"], "score": 0, "answers": []}],
            "started_at": None, "created_at": now_iso()}
    await db.battles.insert_one(room)
    return _battle_public(room, user["id"])


class BattleJoinIn(BaseModel):
    code: str


@api.post("/battles/join")
async def battle_join(inp: BattleJoinIn, request: Request, user: dict = Depends(require_role('student'))):
    await rate_limit(f"battle-join:{client_ip(request)}", limit=20, window_seconds=60)
    room = await db.battles.find_one({"code": inp.code.strip().upper()}, {"_id": 0})
    if not room:
        raise HTTPException(404, "Room not found — check the code.")
    if room["status"] != "lobby":
        raise HTTPException(400, "This battle has already started.")
    if not any(p["user_id"] == user["id"] for p in room["players"]):
        await db.battles.update_one({"id": room["id"]}, {"$push": {"players":
            {"user_id": user["id"], "name": user["name"], "score": 0, "answers": []}}})
        room = await db.battles.find_one({"id": room["id"]}, {"_id": 0})
    return _battle_public(room, user["id"])


@api.post("/battles/{room_id}/start")
async def battle_start(room_id: str, user: dict = Depends(require_role('student'))):
    room = await db.battles.find_one({"id": room_id}, {"_id": 0})
    if not room:
        raise HTTPException(404, "Room not found")
    if room["host_id"] != user["id"]:
        raise HTTPException(403, "Only the host can start the battle.")
    await db.battles.update_one({"id": room_id}, {"$set": {"status": "active", "started_at": now_iso()}})
    room = await db.battles.find_one({"id": room_id}, {"_id": 0})
    return _battle_public(room, user["id"])


@api.get("/battles/{room_id}")
async def battle_state(room_id: str, user: dict = Depends(require_role('student'))):
    room = await db.battles.find_one({"id": room_id}, {"_id": 0})
    if not room:
        raise HTTPException(404, "Room not found")
    state = _battle_public(room, user["id"])
    if state["status"] == "finished" and room["status"] != "finished":
        await db.battles.update_one({"id": room_id}, {"$set": {"status": "finished"}})
    qid = state.pop("_current_qid", None)
    if qid and state["status"] == "active":
        q = await db.questions.find_one({"id": qid}, {"_id": 0})
        me = next((p for p in room["players"] if p["user_id"] == user["id"]), None)
        already = me and any(a["index"] == state["index"] for a in me.get("answers", []))
        state["question"] = _strip_q(q) if q else None
        state["already_answered"] = bool(already)
    return state


class BattleAnswerIn(BaseModel):
    index: int
    selected: List[str] = []


@api.post("/battles/{room_id}/answer")
async def battle_answer(room_id: str, inp: BattleAnswerIn, user: dict = Depends(require_role('student'))):
    room = await db.battles.find_one({"id": room_id}, {"_id": 0})
    if not room or room["status"] != "active":
        raise HTTPException(400, "Battle is not active.")
    state = _battle_public(room, user["id"])
    if state.get("status") != "active" or state.get("index") != inp.index:
        raise HTTPException(400, "That question has expired.")
    me = next((p for p in room["players"] if p["user_id"] == user["id"]), None)
    if not me:
        raise HTTPException(403, "You're not in this battle.")
    if any(a["index"] == inp.index for a in me.get("answers", [])):
        raise HTTPException(400, "Already answered this question.")
    qid = room["question_ids"][inp.index]
    q = await db.questions.find_one({"id": qid}, {"_id": 0})
    correct = answer_is_correct(q, inp.selected) if q else False
    pts = 0
    if correct:
        pts = 100 + max(0, state.get("time_left", 0)) * 5  # speed bonus
    await db.battles.update_one(
        {"id": room_id, "players.user_id": user["id"]},
        {"$push": {"players.$.answers": {"index": inp.index, "correct": correct, "points": pts}},
         "$inc": {"players.$.score": pts}})
    return {"correct": correct, "points": pts, "correct_answer": _correct_letter(q),
            "explanation": q.get("explanation", "") if q else ""}


@api.get("/pyq/filters")
async def pyq_filters(user: dict = Depends(get_current_user)):
    exams = await db.questions.distinct("exam", {"is_pyq": True})
    years = await db.questions.distinct("year", {"is_pyq": True})
    subjects = await db.questions.distinct("subject", {"is_pyq": True})
    chapters = await db.questions.distinct("chapter", {"is_pyq": True})
    total = await db.questions.count_documents({"is_pyq": True})
    return {"exams": sorted([e for e in exams if e]),
            "years": sorted([y for y in years if y], reverse=True),
            "subjects": sorted([s for s in subjects if s]),
            "chapters": sorted([c for c in chapters if c]),
            "total": total}


@api.get("/pyq/search")
async def pyq_search(exam: Optional[str] = None, year: Optional[str] = None,
                     subject: Optional[str] = None, chapter: Optional[str] = None,
                     difficulty: Optional[str] = None, q: Optional[str] = None,
                     page: int = 1, page_size: int = 20,
                     user: dict = Depends(get_current_user)):
    query: dict = {"is_pyq": True}
    if exam: query["exam"] = exam
    if year: query["year"] = str(year)
    if subject: query["subject"] = subject
    if chapter: query["chapter"] = chapter
    if difficulty: query["difficulty"] = difficulty
    if q: query["text"] = {"$regex": q, "$options": "i"}
    total = await db.questions.count_documents(query)
    page = max(1, page); page_size = min(max(page_size, 5), 50)
    docs = await db.questions.find(query, {"_id": 0}).sort([("year", -1), ("subject", 1)]) \
        .skip((page - 1) * page_size).limit(page_size).to_list(page_size)
    # This is a study archive — solutions are meant to be visible.
    items = [{"id": d["id"], "text": d.get("text", ""), "options": d.get("options", []),
              "correct": d.get("correct", []), "explanation": d.get("explanation", ""),
              "exam": d.get("exam"), "year": d.get("year"), "subject": d.get("subject"),
              "chapter": d.get("chapter"), "topic": d.get("topic"),
              "difficulty": d.get("difficulty")} for d in docs]
    return {"total": total, "page": page, "page_size": page_size,
            "pages": (total + page_size - 1) // page_size, "items": items}


class PyqPracticeIn(BaseModel):
    exam: Optional[str] = None
    year: Optional[str] = None
    subject: Optional[str] = None
    chapter: Optional[str] = None
    difficulty: Optional[str] = None
    count: Optional[int] = 15


@api.post("/pyq/practice")
async def pyq_practice(inp: PyqPracticeIn, user: dict = Depends(require_role('student'))):
    query: dict = {"is_pyq": True}
    for f in ("exam", "subject", "chapter", "difficulty"):
        v = getattr(inp, f)
        if v:
            query[f] = v
    if inp.year:
        query["year"] = str(inp.year)
    docs = await db.questions.find(query, {"_id": 0}).to_list(500)
    if not docs:
        raise HTTPException(404, "No previous-year questions match these filters yet.")
    random.shuffle(docs)
    picked = docs[:max(3, min(int(inp.count or 15), 30))]
    qids = [d["id"] for d in picked]
    total = sum(int(d.get("marks", 4)) for d in picked)
    label = " · ".join([x for x in [inp.exam, inp.year, inp.subject, inp.chapter] if x]) or "Mixed"
    now = now_iso()
    test = {"id": new_id(), "title": f"PYQ Practice — {label}"[:80], "exam_type": "pyq",
            "description": "Previous-year questions practice", "subjects": list({d.get("subject") for d in picked}),
            "duration_minutes": max(5, len(qids)), "total_marks": total,
            "negative_marking": True, "shuffle_questions": True, "shuffle_options": False,
            "show_solutions_after": True, "question_ids": qids, "sections": [],
            "assigned_to": [user["id"]], "created_by": user["id"], "created_by_role": "student",
            "personal": True, "owner_id": user["id"], "created_at": now}
    await db.tests.insert_one(test)
    return {"test_id": test["id"], "title": test["title"], "count": len(qids)}


@api.get("/")
async def root():
    return {"app": "ExamNest", "status": "ok"}


@api.get("/health")
async def health():
    try:
        await db.command("ping")
        return {"status": "healthy", "db": "up", "env": os.environ.get("ENV", "dev")}
    except Exception as e:
        raise HTTPException(503, f"db unavailable: {e}")


@api.get("/live")
async def live():
    return {"status": "alive"}


@api.get("/ready")
async def ready():
    try:
        await db.command("ping")
    except Exception:
        raise HTTPException(503, "not ready: db unreachable")
    return {"status": "ready", "db": "up", "env": os.environ.get("ENV", "dev")}


@api.get("/admin/system/integrations")
async def system_integrations(user: dict = Depends(require_role('admin'))):
    import scheduler
    from email_utils import email_mode
    s = ai_status()
    return {"ai": {"source": s["source"], "provider": s["provider"],
                   "mode": "emergent" if ai_full()["emergent"] else "direct"},
            "email": {"mode": email_mode()},
            "scheduler": {"internal": bool(scheduler.jobs()), "jobs": scheduler.jobs()}}




class AISettingsIn(BaseModel):
    provider: Literal['emergent', 'openai', 'gemini', 'claude']
    api_key: str
    model: Optional[str] = None


@api.get("/admin/settings/ai")
async def get_ai_settings(user: dict = Depends(require_role('admin'))):
    return ai_status()


@api.put("/admin/settings/ai")
async def save_ai_settings(inp: AISettingsIn, user: dict = Depends(require_role('admin'))):
    k = inp.api_key.strip()
    if len(k) < 8:
        raise HTTPException(400, "API key too short")
    await db.settings.update_one({"id": "ai"}, {"$set": {
        "id": "ai", "provider": inp.provider, "api_key": k,
        "model": (inp.model or "").strip() or None,
        "updated_at": now_iso(), "updated_by": user["id"]}}, upsert=True)
    await load_ai_cfg(db)
    log.info(f"AI key updated by {user['email']} provider={inp.provider}")
    return ai_status()


@api.delete("/admin/settings/ai")
async def reset_ai_settings(user: dict = Depends(require_role('admin'))):
    await db.settings.delete_one({"id": "ai"})
    await load_ai_cfg(db)
    log.info(f"AI key reset to env default by {user['email']}")
    return ai_status()


@api.post("/admin/settings/ai/test")
async def test_ai_settings(user: dict = Depends(require_role('admin'))):
    await rate_limit(f"ai-test:{user['id']}", limit=5, window_seconds=60)
    cfg = ai_full()
    if not cfg["key"]:
        raise HTTPException(503, "No AI key configured")
    prov, model = cfg["provider"], cfg["model"]
    if not cfg["emergent"] and model in ("gpt-5.4-mini", "gemini-3-flash-preview"):
        model = OWN_DEFAULT_MODEL.get(prov, model)
    t0 = _time.time()
    try:
        txt = (await ai_complete("You are a connectivity probe.", "Reply with exactly: ok", max_tokens=64)).strip()[:60]
        return {"ok": True, "provider": prov, "model": model, "mode": "emergent" if cfg["emergent"] else "direct",
                "latency_ms": int((_time.time() - t0) * 1000), "reply": txt}
    except Exception as e:
        log.warning(f"AI key test failed: {e}")
        raise HTTPException(502, f"Key test failed: {str(e)[:300]}")


app.include_router(api)

# DigiALM-style CBT examination module (self-contained add-on).
from cbt import register_cbt
register_cbt(app, db, get_current_user, require_role)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _capture_unhandled(request: Request, exc: Exception):
    """Sentry-style capture: persist unhandled server errors for the admin
    error feed, then return a clean 500. Expected HTTPExceptions (4xx) are
    handled by FastAPI and never reach here."""
    tb = traceback.format_exc()
    uid = None
    try:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            payload = jwt.decode(auth.split(" ", 1)[1], JWT_SECRET, algorithms=[JWT_ALGO])
            uid = payload.get("sub")
    except Exception:
        pass
    entry = {
        "id": new_id(), "ts": now_iso(),
        "method": request.method, "path": str(request.url.path),
        "error_type": type(exc).__name__, "message": str(exc)[:500],
        "traceback": tb[-4000:], "user_id": uid,
    }
    log.error("Unhandled %s on %s %s: %s", entry["error_type"], entry["method"], entry["path"], entry["message"])
    try:
        await db.error_logs.insert_one(entry)
    except Exception:
        log.error("Failed to persist error_log entry")
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "ref": entry["id"]})


@app.on_event("startup")
async def _startup():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("referral_code")
    await db.questions.create_index("subject")
    await db.attempts.create_index([("user_id", 1), ("status", 1)])
    await db.attempts.create_index([("test_id", 1), ("status", 1)])
    await db.error_logs.create_index([("ts", -1)])
    # CBT: one live attempt per (test, user); fast response lookups.
    try:
        await db.cbt_attempts.create_index(
            [("test_id", 1), ("user_id", 1), ("status", 1)])
        await db.cbt_responses.create_index(
            [("attempt_id", 1), ("question_id", 1)], unique=True)
    except Exception:
        pass
    try:
        await db.rate_limits.create_index("expire_at", expireAfterSeconds=0)
    except Exception:
        pass
    log.info("ExamNest started")
    import scheduler
    scheduler.start(_run_weekly_digests, _run_study_reminders)
    await load_ai_cfg(db)
    log.info(f"AI key source: {ai_status()['source']} provider={ai_status()['provider']}")
    if await db.users.count_documents({}) == 0:
        from seed_data import run_seed
        await run_seed(db)
        log.info("Demo data seeded")
    # ensure a demo teacher exists (idempotent — for DBs seeded before the teacher role existed)
    if await db.users.count_documents({"role": "teacher"}) == 0:
        await db.users.insert_one({
            "id": new_id(), "name": "Rahul Mehta (Teacher)", "email": "teacher1@examnest.io",
            "password": hash_pw("Teacher@123"), "role": "teacher", "created_at": now_iso(),
            "referral_code": "EXNTEACH1", "referred_by": None,
            "reward_coins": 0, "streak_days": 0, "last_active": now_iso(),
            "exam_target": None, "child_ids": [], "parent_ids": [],
            "teacher_perms": {"exams": ["JEE", "NEET"], "subjects": ["Physics", "Chemistry"],
                              "classes": ["11", "12"], "can_print": True, "can_view_results": True},
            "avatar": "https://api.dicebear.com/7.x/initials/svg?seed=Teacher",
        })
        log.info("Demo teacher ensured")
    try:
        from seed_bio_paper import run_bio_seed
        res = await run_bio_seed(db)
        log.info(f"CBSE Biology paper seeded: {res}")
    except Exception as e:
        log.warning(f"Bio paper seed skipped: {e}")
    try:
        from seed_dpp_papers import run_dpp_seed
        log.info(f"DPP visual practice seeded: {await run_dpp_seed(db)}")
    except Exception as e:
        log.warning(f"DPP practice seed skipped: {e}")
    try:
        from seed_cbt_demo import run_cbt_demo_seed
        log.info(f"CBT demo exam seeded: {await run_cbt_demo_seed(db)}")
    except Exception as e:
        log.warning(f"CBT demo seed skipped: {e}")


@app.on_event("shutdown")
async def _shutdown():
    import scheduler
    scheduler.stop()
    client.close()
