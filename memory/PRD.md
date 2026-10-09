# FNJEE.com — Hardened Deployment (ExamNest quiz & mock-test platform)

## Original Problem Statement
"FNJEE — Hardened Deployment Plan": take the uploaded project (FNJEE-main.zip) live safely with rollback + guardrails. Phases: 0 verify stack/env/build, 1 backend health/ready + locked CORS + env config, 2 Mongo auth/TLS/backups/indexes, 3 React prod build + env API URL, 4 HTTPS, rate limiting, secrets store, one-step rollback, smoke test. User: "deploy here".

## Architecture
React 18 (CRA+craco, Tailwind, shadcn) → FastAPI (`server.py` + `cbt.py`) → MongoDB (Motor). JWT+bcrypt auth (4 roles). AI import via emergentintegrations (EMERGENT_LLM_KEY). Crons in `.emergent/crons.yml` (weekly-digest, study-reminder; auth via WEBHOOK_CRON_SECRET).

## Done (2026-06)
- Phase 0: zip extracted & ported into /app (platform .env keys preserved); stack confirmed; deps installed (+6 missing pdf/docx libs), requirements pinned via pip freeze; `yarn build` passes.
- Fail-fast: `JWT_SECRET` and `CORS_ORIGINS` now required (no defaults); ENV=production refuses start if JWT_SECRET < 32 chars or CORS contains '*'.
- Added `/api/live` (liveness) and `/api/ready` (DB ping → 503 if unreachable); `/api/health` kept.
- CORS locked to frontend origin; prod is same-origin via /api ingress.
- Existing: rate limits (login 10/min, submit 20/min), server-side scoring, answer-key stripping, idempotent submit, indexes, error capture to `error_logs`.
- .gitignore no longer excludes .env (deploy blocker).
- Testing iteration_7: backend 14/14, frontend 100%.

## Feature Audit (2026-06, iteration_9 + 10)
- Full audit: backend 49/49 pytest green; frontend 34/34 routes (after fix).
- Fixed: /admin/grading route was missing in App.js (ManualGrader never wired) — now renders in admin layout, verified.
- Working: auth 4 roles, student test flow (timer, server scoring, idempotent submit, no key leak), CBT demo exam, doubt-solve (live LLM), podcasts, flashcards/mindmaps/PYQ/store/referrals APIs, admin question CRUD, analytics, proctoring, system health, AI settings, teacher + parent portals.
- Pending (not bugs): email/weekly-digest needs EMERGENT_EMAIL_KEY (not configured); PUT /api/questions requires full payload (no partial PATCH); production deploy awaiting user's 50 ECU confirm.

## Pending Work Completed (2026-06, iteration_11)
- Email live: EMERGENT_EMAIL_KEY + EMAIL_FROM_NAME="FNJEE.com" in backend/.env; verified real send via managed Resend proxy (delivered@resend.dev). Demo @examnest.io addresses skipped in send_email (fake domain → avoid bounces). Weekly digest + study reminder crons auth-protected (401 without WEBHOOK_CRON_SECRET), ack 200 + background run. Result scorecard emails fire on submit.
- PATCH /api/questions/{qid}: partial update, merged + validated against QuestionIn (422 bad enum, 400 unknown field, 404 missing). Frontend `questionsApi.patch`.
- Readiness: all checks pass except scanner's CORS-wildcard suggestion — intentionally kept locked (user's plan: no '*' in prod; prod is same-origin).
- Testing iteration_11: 12/12 backend, frontend 100%.

## Backlog

## VPS-ready AI layer (2026-06, iteration_12)
- `backend/ai_chat.py::ai_complete(system, text, file_paths, max_tokens)` — single entry for ALL 13 LLM call sites. Emergent key (admin provider 'emergent' or env) → emergentintegrations LlmChat; own OpenAI/Gemini/Claude key → direct REST to official provider APIs (httpx, no Emergent dependency — works on VPS).
- Own-key default models are real provider models (gpt-4.1-mini / gemini-2.5-flash / claude-haiku-4-5-20251001); stale Emergent-catalog names auto-remapped. PDF file inputs supported on all 3 direct providers (base64).
- Key-test endpoint reports mode: emergent|direct. Testing: 17/17 (live emergent regression + mocked direct paths + PDF wiring).

### VPS porting checklist (for user's self-host)
- DONE (iteration_13, 21/21): see /app/DEPLOY_VPS.md.
- Email: email_utils.email_mode() precedence RESEND_API_KEY > SMTP_HOST > EMERGENT_EMAIL_KEY > disabled; guardrail gate on all paths; demo @examnest.io skipped.
- Cron: backend/scheduler.py (APScheduler) when ENABLE_INTERNAL_SCHEDULER=true (set by docker-compose, OFF on Emergent → no double sends). Single worker.
- Infra: docker-compose.yml (mongo:7 auth, no exposed port; backend; Caddy web with auto-HTTPS, /api proxy, cache headers; daily mongodump backup service + deploy/restore.sh). backend/requirements.vps.txt excludes emergentintegrations/litellm (they conflict on public PyPI). frontend Dockerfile rewrites hardcoded preview URLs in index.html/sitemap/robots with SITE_URL.
- Removed leftover emergentintegrations import guards in ai_parser.py (would have broken import on VPS).
- GET /api/admin/system/integrations shows ai/email/scheduler modes.
- Verified by real VPS simulation: clean venv without emergentintegrations boots, scheduler on, SMTP send via local server, own OpenAI key reaches real API.
- Not verified here: docker image builds (no docker in pod), Caddy TLS issuance.
- AI: admin AI Settings → paste own key (done, works). Email: needs own SMTP/Resend (currently Emergent proxy). Crons: need own scheduler (currently platform crons.yml). Mongo: own instance, set MONGO_URL/DB_NAME. Env: JWT_SECRET, CORS_ORIGINS=<own domain>.

## Demo test new tab (2026-06)
- Marketing header "Demo Test" (desktop + mobile) now opens /demo in a new browser tab (`target="_blank" rel="noopener noreferrer"`); landing page tab stays put. Verified via browser automation. Signup CTAs ("Start free mock") unchanged.
- P1: add custom domain to CORS_ORIGINS/APP_BASE_URL when linked; EMERGENT_EMAIL_KEY for digest emails
- P2: split server.py into routers; structured auth logs; staging env; uptime alerts

## Feature: Admin-managed AI key (2026-06)
- `backend/ai_key.py`: admin DB key (`db.settings` id='ai') > `EMERGENT_LLM_KEY` env fallback; provider map (emergent/openai/gemini/claude) → LlmChat provider + default model; module-level cache refreshed on startup + save/reset.
- Endpoints (admin-only): GET/PUT/DELETE `/api/admin/settings/ai`, POST `/api/admin/settings/ai/test` (live probe call, 5/min rate limit). Key stored in Mongo, returned only masked.
- All 13 LLM call sites (server.py ×5, ai_parser ×3, faithful_import, visual_pdf + helpers) now resolve via `ai_key.resolve()` — admin key applies instantly, no restart. Works on VPS with any own key.
- UI: `/admin/settings` (AdminSettings.jsx) — status card (source badge, masked key, model), provider select, password key input, optional model override, Test key, Reset-to-env.
- Testing iteration_8: backend 16/16, frontend save→test→reset flow 100%.

## Fix: Admin file upload failing (2026-10, this session)
- Ported fnjee-main zip into fresh pod; configured env (JWT_SECRET, CORS locked to origin, scheduler off, EMERGENT_LLM_KEY). Verified live: health, auth, core APIs, CORS. DB already had 13 users / 150 questions.
- User's own Gemini key wired via Admin → AI Settings (db.settings id='ai'), mode=direct. Updated stale default model gemini-2.5-flash -> gemini-3.8-flash (2.5 retired for new users).
- ROOT CAUSE of "this file type won't upload on admin panel" (PDF/DOCX MCQ imports):
  1. Gemini 3.8 is a THINKING model (default medium) -> burned output-token budget + huge latency. Fix: generationConfig.thinkingConfig.thinkingLevel="low" in ai_chat._gemini (minimal unsupported on 3.8 flash).
  2. ai_parser extraction used ai_complete default max_tokens=8192 -> JSON array truncated mid-object -> recovery latched onto an inner options[] (strings) -> all filtered -> 0 questions. Fix: max_tokens=32768 for extraction + recovery step 2 now requires arrays of dicts only.
  3. Chunks ran sequentially (MAX_PARALLEL=1, old Emergent single-tier) -> 2 chunks ~65s > Emergent 60s gateway timeout -> 502 "can't upload". Fix: MAX_PARALLEL=5 (own key allows concurrency) -> ~41s, under 60s. (VPS/Caddy has no such timeout anyway.)
  4. Hardened gemini response parse for missing 'parts'/'candidates' (thinking models).
- Verified end-to-end through PUBLIC gateway + admin UI: Hydrocarbons WS.docx -> 76 questions detected with options+answers+subscripts. Chemistry "Solution" PDFs are answer-keys (no stems) -> correctly detected document_kind=solutions, auto-adapt yields practice Qs; use them in the Answer-key slot alongside the DOCX.
- NOTE for VPS: code fixes live in ai_chat.py + ai_parser.py (port with repo). Gemini key is DB-stored -> re-enter in Admin → AI Settings on the fresh VPS DB. A reboot alone does NOT fix the upload; the code changes do.

## Feature: Batch categorisation in Import Wizard (2026-10, this session)
- Added "Categorise imported questions (optional)" panel to Import Wizard step 1 (ImportWizard.jsx): Type, Difficulty, Status, Chapter, Topic, Section, Exam, Class, Year, Tags (comma-sep), Marks, Negative.
- Behaviour: any field the admin sets is applied to EVERY question in that file (overrides detected values); blank/"auto" keeps AI/regex-detected values. Import works with or without any selection.
- Backend import_parse (server.py): new optional Form params (*_default) + override loop applied to parsed[] before answer-key/dup stages; tags merge-union. All fields persist via QuestionIn on commit.
- Verified: API applied all 12 fields to 76 questions (HTTP 200); UI panel renders with testids cat-type/difficulty/status/chapter/topic/section/exam/class/year/tags/marks/negative.

## Fix: "Unable to upload any DOC or PDF" — async import (2026-10, this session)
- ROOT CAUSE: Emergent gateway hard-kills any single HTTP request at 60s. AI extraction of PDFs/large DOCX takes 60-130s, so the synchronous POST /api/import/parse returned 502. (On VPS/Caddy there's no 60s cap, but preview + any slow file failed.)
- FIX (background job): POST /api/import/start returns {job_id} instantly, server runs AI in the background (_run_import_job), GET /api/import/jobs/{job_id} is polled by the frontend every 2.5s until status=done. Each HTTP request is now <1s -> no gateway timeout. Refactored shared core into _do_import_parse() + _resolve_import_inputs(); /import/parse kept for paste/sync. Background task refs held in _bg_tasks to avoid GC.
- FIX (LaTeX JSON): solutions PDFs with chemistry/LaTeX (\sigma, \pi, \frac) produced count=0 because AI JSON had invalid backslash escapes -> json.loads 'Invalid \escape'. Added escape sanitiser (double any backslash not starting a valid JSON escape) in visual_pdf.py and ai_parser._loads_lenient (3 recovery sites).
- Frontend: importApi.start + importApi.jobStatus; ImportWizard.runParse now starts job + pollImportJob() with 10-min ceiling.
- VERIFIED by testing_agent iteration_1 + iteration_2 (100%, 5/5): DOCX e2e 75 Q (UI review rows + commit), solutions PDF 40 adapted Q (was 0), no 502, categorisation applied.

## Fix: HTTP 413 on VPS + Universal never-error import (2026-10, this session)
- 413 ROOT CAUSE: app + Caddy + Emergent gateway all accept large uploads (verified 25MB->200). 413 came from an external reverse proxy on the user's VPS (nginx default client_max_body_size=1MB). FIX (config): frontend/Caddyfile adds `request_body { max_size 60MB }` on /api/*; deploy/nginx-fnjee.conf (client_max_body_size 60m) + DEPLOY_VPS.md troubleshooting section. Verified app-side immunity 100%.
- UNIVERSAL NEVER-ERROR IMPORT: _do_import_parse rewritten to (STEP1) code-first deterministic parse — parse_excel (xlsx) / extract_text_any -> regex (docx,pdf,pptx,pagemaker,txt,csv,html,rtf,md); (STEP2) AI fallback when code yields <3 OR image OR adapt — PDFs/images via parse_visual_pdf vision (images wrapped to PDF by parsers.image_to_pdf), other text via ai_extract_questions. All steps wrapped: NEVER raises, background job always ends status='done'; unreadable files return count=0 + friendly note (no 500/4xx). New parsers: parse_pptx, parse_markup, image_to_pdf, extract_text_any. Added dep python-pptx==1.0.2. Frontend accept list expanded to images/pptx/csv/html/rtf.
- VERIFIED testing_agent iter3 (10/10) + iter4 (8/8, 100%): TXT regex, PPTX, PNG vision, DOCX 60+, solutions PDF adapt, garbage weird.xyz graceful, 20MB->200, login role.
- Live progress plumbing (job.progress {pct,msg}) in backend + bulk endpoints (/questions/bulk-update, /questions/bulk-delete) done & tested. FRONTEND still pending: progress-bar display, folder/multi-file import, auto answer-key pairing UI, Question Bank filters+bulk-edit UI.

## Fix: Word import works in preview but fails on VPS (2026-10, this session)
- ROOT CAUSE: AI (Gemini) key was stored ONLY in the DB (Admin -> AI Settings). VPS has a fresh/empty DB -> no AI key -> chemistry .docx (regex finds 0) -> AI fallback had no key -> 0 questions = "not working". Preview worked only because the key was saved in the preview DB.
- FIX: ai_key.py now falls back to an ENV provider key. Priority: DB admin key > GEMINI_API_KEY (or OPENAI_API_KEY / ANTHROPIC_API_KEY) env > EMERGENT_LLM_KEY. On a VPS set GEMINI_API_KEY in backend/.env; AI then runs via direct HTTPS to Google (ai_chat._gemini), no emergentintegrations needed (imported lazily only for the Emergent branch). status()/source reports 'env-key'.
- Docs: deploy/backend.env.example now documents GEMINI_API_KEY as the recommended VPS way; EMERGENT_LLM_KEY noted as Emergent-platform-only.
- VERIFIED testing_agent iter5 (6/6, 100%): with DB 'ai' doc deleted + GEMINI_API_KEY env only (mirrors VPS) -> AI status source='env-key', /ai/test mode='direct' ok, DOCX import count>=60, PDF import count>0, DB override still wins then cleaned up.
- Preview is intentionally left in env-key-only state (no DB ai doc) to mirror VPS.

### ACTION FOR USER ON VPS: add to backend/.env ->  GEMINI_API_KEY="<your Google AI Studio key>"  then `docker compose restart backend` (or redeploy).
