# Tee:dentity live smoke run

Date: 2026-10-26  
Environment: local FastAPI + Vite app, Portkey course key, all model roles reported as `gpt-6-luna`  
Smoke session: `BIXbr8NxF-am`  
Country: South Korea (KR)

## Result

The live smoke script completed **24/24 steps successfully**. API call time totaled 150 seconds.

- Health check: HTTP 200; key presence reported true; model map showed `gpt-6-luna` for all roles.
- Quiz: all 10 rounds answered; final session reported `quiz_done=true`, 10 answers, and no hidden engine fields in the public state.
- Quiz observations: 14/20 options had images; 6/10 rounds had native-script sublabels; answer latency median 3.3 s, max 4.7 s, with 1/9 non-final answers under 3 s.
- About-you: synthetic Seoul interests note and the public Seoul Wikipedia URL both returned `ok`.
- Profile: “The Euljiro Night-Ride Collector”; 7 traits and 5 palette colors.
- Studio: 3 designs generated and a requested revision returned successfully.
- Demo order: draft created for 12 shirts at $280.80, request recorded, and print SVG returned as `image/svg+xml` (4 KB).
- Shop: 10 suggestions returned across local (2), global (3), and indie (5); 8/10 cards had images.
- Token usage endpoint: 294,686 input tokens (61,950 cached input), 27,107 output tokens, 37 usage records, estimated token cost **$0.0374466**.

The cost estimate applies the course rates ($0.10 per million uncached input tokens, $0.01 per million cached input tokens, and $0.50 per million output tokens). Hosted web-search fees are excluded.

## Scope and limits

This is a synthetic end-to-end smoke run for functionality and token accounting. The note, URL, profile, order, and timings are not evidence of real-user impact or a controlled performance study. No screenshot was captured during this run. The original console output showed HTTP 200 for each smoke step; preserve a terminal capture or screenshot from a rerun if the report needs visual evidence.

## Follow-up browser captures

After the scripted run, Tee:dentity reopened this saved session in the local UI to capture Profile, usage, and Studio screens. Opening Profile starts background shop prefetch work. The session's final usage endpoint read after that work completed was **43 requests, 549,558 input tokens (83,600 cached), 34,286 output tokens, and $0.0645748 estimated token cost**. The earlier 37-request / $0.0374466 figures above are the standalone scripted-smoke boundary and remain the basis for the per-run scenarios in the report. The added browser-triggered requests were not timed or attributed by role.

Screenshot files: `app_profile_2026-10-26.jpg`, `app_usage_2026-10-26.jpg`, and `app_studio_2026-10-26.jpg`. The workflow diagram in the report is derived from the current code and smoke steps; the app does not expose a per-agent execution trace.
