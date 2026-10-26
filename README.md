# Tee:dentity

Tee:dentity is a country-aware 10-round this-or-that game. It uses a player's choices and optional
personal context to build a taste profile, find real T-shirts, and generate custom shirt designs
with a demo order flow. Shop cards label whether a chosen game pick appears in retailer page metadata or only in the search result title. The app uses FastAPI, SQLite, React, TypeScript, and Vite. Every model role
is fixed to **`gpt-6-luna` through Portkey** to comply with the course key.

## Start from a fresh clone

You need Python 3.13, Node.js 24 or newer with npm 11, Git, and your course Portkey key.

1. Clone the project and enter its folder.
2. Make a project-local `.env` from the template and add your key. You can instead keep the course
   `.env` one folder above the project; the backend checks that location too.
3. Install the backend and frontend dependencies.
4. Start the API, then start the web app in a second terminal.

### Windows PowerShell

```powershell
git clone https://github.com/boonpilgaru-ux/MGT409_Final_Project_Seungwon-Shin.git teedentity
cd teedentity
Copy-Item .env.example .env
# Edit .env and set PORTKEY_API_KEY to your course key.
py -3.13 -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd frontend
npm ci
cd ..
```

Terminal 1, from `teedentity`:

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Terminal 2, from `teedentity`:

```powershell
cd frontend
npm run dev
```

Open <http://localhost:5173>. On Windows, `powershell -ExecutionPolicy Bypass -File .\run.ps1`
also creates the venv, installs dependencies, and starts both servers.

### macOS or Linux

```bash
git clone https://github.com/boonpilgaru-ux/MGT409_Final_Project_Seungwon-Shin.git teedentity
cd teedentity
cp .env.example .env
# Edit .env and set PORTKEY_API_KEY to your course key.
python3.13 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt
cd frontend && npm ci && cd ..
```

Terminal 1, from `teedentity`:

```bash
cd backend
./.venv/bin/python -m uvicorn app.main:app --port 8000
```

Terminal 2, from `teedentity`:

```bash
cd frontend
npm run dev
```

Open <http://localhost:5173>. The Vite dev server proxies `/api` to <http://127.0.0.1:8000>.
Check <http://127.0.0.1:8000/api/health>; it reports whether the key is present and lists the
configured model names without exposing the key.

## Configuration and privacy

- `PORTKEY_API_KEY` is required for live AI features. `.env.example` has no secret value.
- The backend checks `backend/.env`, the project `.env`, the project parent's `.env`, then a legacy
  shared `.env` two levels above the project. Environment variables take precedence. Never commit
  a real `.env` or publish a key.
- All model roles, including photo judging and search fallback, use `gpt-6-luna`. Model overrides
  are intentionally disabled so a course key cannot be routed to another model.
- `TEE_DB_PATH` optionally changes the SQLite location. By default, data stays in
  `backend/data/app.db` inside the project.
- The app keeps answer/context/profile data in the local SQLite session so a player can resume.
  Uploaded file contents and prompts are not included in token usage records. “Start over” deletes
  the session data and its usage totals; session retention defaults to 30 idle days.

Optional settings are listed in `.env.example`. For a different API port, set `TEE_API_URL` for the
Vite process (the Windows `run.ps1` script sets this automatically).

## Token usage and cost

The API records session-level input tokens, cached input tokens, output tokens, request count, and
an estimated cost in `backend/data/app.db`. View the totals on the Profile page or fetch
`GET /api/usage/{session_id}`. Only usage totals are stored; prompts and model responses are not.

The estimate uses the course rates per million tokens: $0.10 uncached input, $0.01 cached input,
and $0.50 output. Cached input is included in the input-token total. Hosted web-search fees are not
included, and the estimate depends on usage data returned by Portkey. Set a Portkey budget limit for
the key before public deployment; the per-IP rate limit is not a total spending cap.

## Checks and real API smoke run

Offline backend tests and frontend checks:

```powershell
# from teedentity/backend
.\.venv\Scripts\python.exe -m pytest
# from teedentity/frontend
npm run typecheck
npm run build
```

```bash
# from teedentity/backend
./.venv/bin/python -m pytest
# from teedentity/frontend
npm run typecheck
npm run build
```

For a real example, start the API and run the opt-in live smoke test from `backend/`. This makes
real, billable calls through the configured Luna key and exercises the 10-round journey, profile,
design, demo order, and optionally shop search. Use `--skip-shop` to omit the live web search.

```powershell
.\.venv\Scripts\python.exe scripts\smoke_live.py --skip-shop
```

```bash
./.venv/bin/python scripts/smoke_live.py --skip-shop
```

A real successful run is summarized in [evidence/live_smoke_2026-10-26.md](evidence/live_smoke_2026-10-26.md). It is a synthetic smoke run, not user-impact evidence. Do not report fabricated runs, costs, screenshots, or user feedback.

## Safety and remaining evidence

The app limits costly requests per client IP, blocks private and local network destinations while fetching URLs, labels user-supplied material as untrusted data in model prompts, and sanitizes generated SVGs. The live smoke run is documented in [evidence/live_smoke_2026-10-26.md](evidence/live_smoke_2026-10-26.md); its 24/24 result is a synthetic functional run, not user-impact evidence.

The screenshots in `evidence/` are local captures from the saved synthetic session. The scripted smoke originally measured 37 model requests and $0.0374466. Opening Profile for browser captures started six background requests, so the same saved session later read 43 requests and $0.0645748. Those are separate measurement boundaries. The app records usage by session, but does not separate it by role or expose a live agent trace.

The safety output in [evidence/safety_checks_2026-10-26.txt](evidence/safety_checks_2026-10-26.txt) shows 19 prompt-injection, private-network redirect, and malicious-SVG test cases passing. The tests use local fakes; they are not tests of the live provider's response to hostile content. No real-user feedback, controlled usability study, or measured impact is available. Rate limiting does not impose a total spending cap.

The course submission includes `report.html` (12 print pages) and `slides.html` (9 Reveal.js slides). There is no public hosted app; run it locally at <http://localhost:5173>.

## Main folders

- `backend/app/` — API, quiz, profile, shop, design, order, safety, and persistence code.
- `backend/tests/` — offline automated tests.
- `backend/scripts/smoke_live.py` — opt-in live end-to-end smoke script.
- `frontend/src/` — React pages, components, and API client.
- `SPEC.md` — product and API design reference.
