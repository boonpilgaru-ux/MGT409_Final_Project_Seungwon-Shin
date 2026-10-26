# Tee:dentity — product + technical spec (source of truth for all builders)

A web app that learns who you are through a 10-round **balance game** (밸런스게임), grounded in
your country's culture, then (a) finds real t-shirts you can buy that express you and (b) designs a
personalized t-shirt and produces a shareable order link.

## 0. Ground truth about the environment (already verified)

- All model calls use Portkey's OpenAI-compatible API at `https://api.portkey.ai/v1` with
  `PORTKEY_API_KEY`; the key is never printed, logged, or returned from an API endpoint.
- Course-key compliance: all model roles (quiz, profile, shop, design, summaries, image judging, and
  search fallback) are fixed to **`gpt-6-luna`**. Model overrides are disabled.
- The backend checks `.env` in `backend/`, the project root, its parent, and a legacy shared course
  location two levels above the project. Real environment variables take precedence.
- The app uses the Responses API with hosted `web_search` for shop discovery. Image generation is
  unavailable through the course gateway; shirt art is created as sanitized SVG.
- Tooling: Python 3.13 and Node.js 24+ with npm 11. The app can be run on Windows, macOS, or Linux.

Usage totals are stored separately from prompts and responses. The profile's cost estimate uses
the course rates ($0.10/1M uncached input, $0.01/1M cached input, $0.50/1M output) and excludes
hosted web-search fees.

## 1. Stack and layout

```
teedentity/
  SPEC.md
  README.md                  how to run
  backend/
    requirements.txt         fastapi, uvicorn[standard], openai, python-dotenv, pydantic>=2, httpx,
                             python-multipart, pypdf, python-docx, beautifulsoup4, pytest
    app/
      __init__.py
      main.py                FastAPI app, CORS for localhost:5173, mounts routers under /api, GET /api/health
      config.py              env loading, model names, Portkey client factory (sync + async OpenAI clients)
      llm.py                 helpers: chat_json(model, system, user, schema_model) -> pydantic obj
                             (robust JSON extraction from fenced/unfenced text, 1 repair retry), web_search_json(...)
      store.py               SQLite (data/app.db) session/order persistence; JSON blobs; thread-safe
      models.py              ALL pydantic request/response models (see section 4) — single source of truth
      images.py              option image search (Openverse + Wikimedia), cache, scoring, fallback=None
      quiz.py                balance-game question generation + speculative prefetch
      context.py             uploads (pdf/docx/txt/md) + URL ingestion (SSRF-safe) + summarization
      docparse.py            pdf/docx text extraction in a killable child process, with size caps
      ratelimit.py           per-client token bucket for the costly endpoints (429 + Retry-After)
      profile.py             personality/taste profile synthesis
      shop.py                real t-shirt discovery via web search, link validation, og:image enrichment
      design.py              SVG graphic generation + sanitization
      orders.py              order creation, pricing estimate, print file export
      routes.py              all API routes (thin; call modules above)
    tests/                   pytest; LLM + network mocked
    scripts/smoke_live.py    live end-to-end run against real Portkey (opt-in)
  frontend/                  Vite + React 18 + TypeScript, react-router-dom v6, no UI framework (plain CSS)
    vite.config.ts           dev server port 5173, proxy /api -> http://127.0.0.1:8000
    src/
      main.tsx, App.tsx      router + layout + session bootstrap
      api.ts                 typed fetch wrappers for every endpoint in section 4
      types.ts               TS mirrors of section 4 models (exact field names)
      session.ts             session id in localStorage ("tee.session"), shortlist in localStorage ("tee.shortlist")
      countries.ts           full ISO-3166 country list: {code, name, flag emoji}
      styles.css             design tokens + global styles
      components/            OptionCard, VsBadge, ProgressBar, TeeMockup, Spinner, Stepper, etc.
      pages/                 Landing, Quiz, AboutYou, Profile, Shop, Studio, Order
```

Run: backend `uvicorn app.main:app --port 8000` from `backend/` (venv at `backend/.venv`);
frontend `npm run dev` from `frontend/`.

## 2. User flow

1. **Landing / Country** (`/`): hero + searchable country picker (flag + name). Required. Creates session.
2. **Balance game** (`/quiz`): 10 rounds. Each round = one A/B question. After each answer the
   server generates the next question adaptively. Ends → `/about-you`.
3. **About you** (`/about-you`), optional, skippable: upload resume/essays (pdf, docx, txt, md; ≤10 MB each,
   ≤5 files), paste SNS/portfolio URLs (≤5), free-text "anything else" (≤5 notes). Shows each item's status +
   1-line summary.
4. **Profile** (`/profile`): reveal card — archetype name, tagline, summary, trait bars, taste palette
   (color swatches, aesthetics, motifs), cultural roots, "your t-shirt brief". CTAs: "Find shirts" and "Design mine".
5. **Shop** (`/shop`): 8–12 real, currently-purchasable t-shirts from real retailers (mix of: brands from the
   user's country, global brands, indie/independent makers). Card: image, title, brand, price, retailer, a style note, and a pick-match badge. A page match means the selected answer appears in retailer title/description metadata; a search-title match is shown as a possible match.
   Actions: ♥ shortlist (localStorage), **Buy** (opens retailer product page in new tab, `rel="noopener noreferrer"`).
   Shortlist drawer with Buy buttons. "Refresh suggestions" re-runs search.
6. **Studio** (`/studio`): personalized design. Starts by generating 3 concept graphics from the profile.
   Pick one; iterate via feedback text ("bolder", "add Hangul", "more minimal"). Configure product:
   shirt color, fit, material, neckline, sleeve, size, print method, placement, quantity, notes.
   Live SVG t-shirt mockup reflects color/fit/placement. **Create order link** → `/order/:id`.
7. **Order** (`/order/:id`): shareable page: mockup, full spec sheet, price estimate, design concept, download
   print-ready SVG, copy-link button, "Place order" form (name, email, address) that records the order with
   status `requested` (no real payment — clearly labeled "demo checkout; no charge"). Anyone with the link can
   view it and place the request once; the public view never shows the buyer's email or address, and only the
   browser that placed the request can update it (others see "already requested" and "Make your own").

Navigation: top stepper (Country → Game → About you → Profile → Shop / Studio). Session restores on refresh
via `GET /api/session/{id}`. If session missing → back to `/`; if the server can't be reached, show a retry
banner and never silently replace the stored session. "Start over" deletes the session on the server too
(`DELETE /api/session/{id}`; orders stay so shared links keep working). Sessions idle for 30 days are purged.
The landing page pre-generates round 1 for the chosen country (`POST /api/quiz/warm`), and the profile page
starts the shop search and the first 3 design concepts in the background as soon as the profile is shown (if
the profile is regenerated before Studio is first opened, Studio draws fresh concepts for the current one).

## 3. Balance game design (the heart of the app — get this right)

**What a 밸런스게임 is:** a Korean party/online game: "A vs B — you must pick one." Characteristics:
- Two options, **both genuinely tempting (or both painful)**, so the choice reveals priorities. Never an obvious answer.
- Short, punchy, concrete, vivid. Often hypothetical "for the rest of your life" / "would you rather" framing.
  e.g. "평생 짜장면만 vs 평생 짬뽕만", "여름에 패딩 입기 vs 겨울에 반팔 입기", "10억 받고 무인도 1년 vs 그냥 살기",
  "Travel: plan every hour vs no plan at all", "Concert front row alone vs back row with best friends".
- Playful, humorous, relatable; uses very local cultural references (foods, places, transit, school life,
  holidays, pop culture, slang) — that's why the **country is the key input**.
- Ranges from light/general to oddly specific.
- About one round in three uses a twist: "forever", "would you rather", a perk with a catch, or both options a
  little painful. Every third round is a required twist round; its type comes from a per-session shuffled order
  ("forever" skipped once it is used up), each option must carry a cost (or both be a little painful), and the
  model reports the `twist` it used.

**Our adaptive engine (quiz.py):**
- Total = 10. Depth curve: rounds 1–3 `broad` (lifestyle, energy, social, time), 4–7 `focused` (aesthetics,
  values, humor, risk, nostalgia vs novelty, craft vs convenience), 8–10 `specific` (narrow, culturally specific,
  taste-level details that directly inform t-shirt design: colors, typography vibes, motifs, eras, subcultures,
  fabric feel, fit, logos vs no logos, wearable statement level). Specific rounds read that taste through culture
  and everyday life (a shop sign, an album cover, a room); they are not a design survey: at most 1–2 prompts per
  game mention the shirt.
- Each question targets a **dimension** chosen to maximize information given prior answers (avoid repeating
  dimensions; probe ambiguity; follow up on surprising answers with a different object). The LLM receives:
  country, full Q/A history (prompt, both options, chosen side, dimension), round index, depth target, a running
  hypothesis, dimension ideas in a per-session order, and local references to avoid (used in this game, in the
  omitted style examples of the player's country, and recently by other players from that country). Style
  examples from the player's own country are left out of the prompt so games aren't cloned from them.
- Every question is **grounded in the user's country**: at least ~2/3 of questions should use a locally specific
  reference (real local foods/places/brands/customs/media), and only a reference actually shown on the cards
  counts. Options written in English; local terms may appear with native script in `sublabel` (e.g. label
  "Tteokbokki", sublabel "떡볶이"). The sublabel names the thing (or one concrete detail of it), never the trait
  it stands for, and in broad/focused rounds the label doesn't either (not "Beautifully useless art object" for
  "function vs beauty"); names must be the real ones locals use. No country names or nationality words in prompts
  or labels unless part of a proper name ("Mexico City"). Never the same object twice in a game, even reworded
  (a shared multi-word proper name or 2+ shared content words with an earlier sublabel or photo query is a soft
  issue: "Lucha libre" then "Máscara de lucha libre").
- Options must be **balanced** (no socially "correct" answer), parallel in form, each ≤ 7 words label,
  sublabel ≤ 12 words optional. Prompt ≤ 12 words (e.g. "Pick one. Forever.", "Saturday night:"), fits both
  options, and never asks "X or Y?" (a prompt that still does is cut to its frame).
- Safety: no politics/religion/sexual content, no harmful stereotypes, no tragedy jokes. Light, kind, fun.
- Output per option includes `image_query` (2–5 English words, concrete and photographable, e.g. "bowl of jjajangmyeon")
  + `emoji` + `color` (hex) for fallback.
- The LLM also returns an updated `hypothesis` (≤ 60 words, hidden from user) stored in the session.
- **Speculative prefetch:** when question N is served, the backend starts generating question N+1 for BOTH
  choices (A and B) in background asyncio tasks (including image lookup). On answer, it awaits the matching task
  (or generates fresh if missing/failed). This makes the next question near-instant. Cancel the losing task.
  Drafts with only minor (soft) rule issues get a corrective retry only while nobody waits for that round; once
  the player is waiting, the usable first draft is served. Round 1 is pre-generated per country (warm) and
  handed to exactly one session.
- Image lookup (images.py): search with `image_query`; prefer near-square images ≥ 400px wide, jpg/png/webp, skip svg/gif/tif;
  try Openverse then Wikimedia; per-request timeout 4 s; LRU cache by query; return `{url, credit, source_url}` or `null`.
  Never a sensitive photo (war, military, politics and royalty, religion and places of worship, tragedy/memorials/slavery, nudity;
  matched on title, tags and description); the query's first word (its subject) must match, its head noun too
  unless the title carries a multi-word proper name from it, and proper nouns match whole ("Lagos" is not
  "Lago"). Photos that name the player's country, or are described in the card's native script, rank first.
  Both options' images fetched concurrently, never repeating a photo already shown in the game; if only one
  option finds a photo, the other gets one more try, else both use the fallback (balanced cards). Frontend
  renders cards at identical fixed aspect ratio (1:1), `object-fit: cover`; if `image` is null render a gradient
  (option color) with big emoji.

## 4. API contract (all JSON unless noted; prefix /api)

Errors: `{ "detail": string }` with proper HTTP status. LLM failures → 502 with friendly detail. Costly endpoints
(session, quiz warm/start/answer, context, profile, shop, design, orders) are rate limited per client IP → 429 with
`Retry-After`. Requests with a Host header outside `TEE_ALLOWED_HOSTS` → 400 (DNS-rebinding guard).

### Models (pydantic in backend/app/models.py; TS mirror in frontend/src/types.ts — EXACT same field names)

```
Country        { code: str (ISO alpha-2), name: str }
OptionImage    { url: str, credit: str, source_url: str | null }
Option         { label: str, sublabel: str | null, emoji: str, color: str (hex), image: OptionImage | null }
Question       { id: str, index: int (1-based), total: int (10), prompt: str,
                 depth: "broad" | "focused" | "specific", option_a: Option, option_b: Option }
Answer         { question_id: str, choice: "A" | "B" }
ContextItem    { id: str, kind: "file" | "url" | "text", name: str, chars: int,
                 status: "ok" | "limited" | "error", summary: str, error: str | null }
Trait          { name: str, score: int (0-100), low_label: str, high_label: str, description: str }
Taste          { colors: [ {hex: str, name: str} ] (4-6), aesthetics: [str], motifs: [str],
                 typography: str, fits: [str], avoid: [str] }
Profile        { archetype: str, tagline: str, summary: str, traits: [Trait] (5-7), taste: Taste,
                 cultural_roots: str, values: [str], tshirt_brief: str }
Product        { id: str, title: str, brand: str, retailer: str, url: str, image_url: str | null,
                 price: str | null, currency: str | null, origin: "local" | "global" | "indie",
                 why: str, pick_match: str | null, pick_match_source: "product_page" | "search_result" | null }
ProductSpec    { shirt_color: str (hex), fit: "regular"|"oversized"|"slim"|"boxy"|"cropped",
                 material: "cotton"|"organic_cotton"|"heavyweight_cotton"|"tri_blend"|"linen_blend"|"recycled_poly",
                 neckline: "crew"|"v_neck"|"mock_neck", sleeve: "short"|"long"|"sleeveless",
                 size: "XS"|"S"|"M"|"L"|"XL"|"XXL", print_method: "dtg"|"screen"|"embroidery"|"puff",
                 placement: "front_center"|"left_chest"|"back"|"front_and_back", quantity: int (1-50),
                 notes: str (≤500) }
Design         { id: str, title: str, concept: str, svg: str (sanitized, viewBox square), palette: [str],
                 recommended_spec: ProductSpec }
Order          { id: str, created_at: str (ISO), status: "draft" | "requested", design: Design, spec: ProductSpec,
                 price: { unit: float, total: float, currency: "USD", breakdown: [ {label: str, amount: float} ] },
                 profile_archetype: str | null, country: Country | null,
                 customer: { name: str, email: str, address: str } | null }
                 (GET /api/orders/{id} is public: customer.email is masked "m•••@example.com" and
                 customer.address is "(hidden)"; the full details are stored for fulfilment)
SessionState   { id: str, country: Country, answers: [ {question: Question, choice: "A"|"B"} ],
                 current_question: Question | null, quiz_done: bool, context: [ContextItem],
                 profile: Profile | null, designs: [Design] }
```

### Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | /api/health | – | `{ok: true, portkey_key_set: bool, models: {...}}` |
| POST | /api/session | `{country: Country}` | `SessionState` |
| GET | /api/session/{id} | – | `SessionState` (404 if unknown) |
| DELETE | /api/session/{id} | – | 204 (extra: "Start over"; deletes the session, keeps its orders) |
| POST | /api/quiz/warm | `{country: Country}` | 202 (extra: pre-generate round 1 for that country) |
| POST | /api/quiz/{id}/start | – | `Question` (idempotent: returns current question if already started) |
| POST | /api/quiz/{id}/answer | `Answer` | `{next: Question | null, done: bool}` (409 if question_id ≠ current) |
| POST | /api/context/{id} | multipart: `files[]` (0-5), `urls` (JSON string array), `text` (str) | `{items: [ContextItem]}` (all items so far; 411 without Content-Length or with Transfer-Encoding, 413 if the body is, or turns out to be, over 5×10 MB + 2 MB; costs 1 rate-limit token + 1 per PDF/DOCX file) |
| DELETE | /api/context/{id}/{item_id} | – | `{items: [ContextItem]}` |
| POST | /api/profile/{id} | – | `Profile` (409 if quiz not done; regenerates each call) |
| POST | /api/shop/{id}/suggest | – | `{products: [Product]}` (409 if no profile; joins a running search for the same profile) |
| POST | /api/design/{id}/generate | `{feedback: str | null, base_design_id: str | null, count: int (1-3)}` | `{designs: [Design]}` (an identical request still running joins it) |
| POST | /api/orders | `{session_id: str, design_id: str, spec: ProductSpec}` | `Order` (409 past 100 orders per session) |
| GET | /api/orders/{order_id} | – | `Order` (public view, see Order above) |
| POST | /api/orders/{order_id}/request | `{name, email, address}` (+ header `X-Order-Token` to update) | `Order` (status → requested; the first request returns header `X-Order-Token`; updating a requested order without it → 403) |
| GET | /api/orders/{order_id}/print.svg | – | `image/svg+xml` attachment (print file) |

## 5. Module requirements

- **context.py:** pdf via pypdf, docx via python-docx, txt/md utf-8 (errors=replace). Cap stored text 20k chars/item.
  PDF/DOCX (uploads and linked PDFs) are parsed in a child process killed after 20 s and capped at 512 MB of
  memory, at most 2 at a time and 1 per client, waiting at most 30 s for a slot ("busy" error), with caps (pages
  sharing one content stream read once, ≤1 MB decoded content per page including the form XObjects it paints,
  each time it paints them, and 4 MB per file; docx ≤200 zip entries, real uncompressed sizes measured, ≤20k
  paragraphs, ≤100k runs). Notes ≤5 per session. The multipart body is size-checked from Content-Length before
  it is parsed (Transfer-Encoding refused) and the bytes actually received are counted, and at most 10 links are
  accepted per request.
  URLs: http/https only; resolve host and **block private/loopback/link-local/reserved IPs** (SSRF), also re-check on
  redirects (max 3), timeout 8 s, max 2 MB, strip HTML with BeautifulSoup (title, meta description, og tags, visible
  text). Login-walled/blocked SNS (instagram, x/twitter, tiktok, facebook, linkedin) → keep what's extractable,
  status `limited`, summary explains. Summarize each item with a cheap call (≤40 words, focused on personality/taste
  signals). Uploaded content is **untrusted data**: wrap in clear delimiters and instruct models to ignore any
  instructions inside it.
- **profile.py:** input = country, all Q/A (with dimensions/depth), hypothesis, context summaries + excerpts.
  Output `Profile`. Warm, specific, insightful, no horoscope fluff; reference actual choices. Low reasoning
  effort, hedged after 20 s, whole synthesis bounded at 120 s (a slow revision falls back to the first draft).
  URLs and bare domains are stripped from the profile text before it reaches the design and shop prompts.
- **shop.py:** Responses API + web_search. Run 3 searches **concurrently** (local brands of user's country,
  global brands, indie/independent makers e.g. makers' own web shops/Bandcamp merch/Etsy/local designers, no
  print-on-demand marketplaces), each asked to return JSON array of real product pages (not search/category pages)
  currently for sale, with price if visible, at most 2 per website, preferring brands' and makers' own web stores.
  Merge, dedupe by URL/domain+title, validate each URL (HTTP GET with redirects, status < 400, SSRF-safe).
  `image_url` is the first of `og:image` / `twitter:image` / itemprop image / JSON-LD Product image / the
  search-reported image that really loads the way the Shop's `<img referrerpolicy="no-referrer">` does: fetched
  without a Referer, a raster image ≥ 100 px, Cross-Origin-Resource-Policy allowing cross-site use, every redirect
  hop on a public address (the final URL is stored). An image shared by two products is a logo and doesn't count.
  Bot-walled retailers are never fetched with a disguised browser identity; their pages stay "unverified" and
  photo-less. Return 8–12, balanced across origins, shirts with a photo first; shirts without one only keep an
  origin represented, fill up to 8, and are otherwise at most a quarter of the list. Total timeout ~75 s; partial
  results OK: once 8 products are verified and two origins are done, the last origin gets ~6 s more instead of the
  rest of the budget (plus ~4 s to check its links if its search answers within them). A hedged backup search
  that lost its race still adds its shirts if it answers in time (while fewer than 8 shirts have a photo, the run
  waits up to those ~6 s for it). A product link that redirects to a listing page counts as dead.
- **design.py:** LLM produces a print graphic as standalone SVG (square viewBox 0 0 1000 1000, transparent background,
  ≤ 6 colors, flat vector shapes + text; text must use generic font families; no raster images). Inspired by the
  profile's taste/motifs/cultural roots. `count` concepts generated concurrently with distinct directions
  (typographic, illustrative emblem, abstract pattern) plus one spare in a further direction (line art). The spare
  is kept only in place of a direction that failed, or that is still drawing ~5 s after `count` concepts are
  ready, so one broken or stalled concept costs the batch neither a design nor much time. Each call is hedged
  after 30 s; each concept that is ready gives the rest a ~15 s grace period to deliver the next one, then the
  batch returns with what's done. A concept whose reply is unusable gets one corrective retry, started at once
  but only if the batch would otherwise come up short (the spare covers a single failure). Feedback mode
  revises `base_design_id` (no spare).
  **Repair before rejecting:** an SVG that isn't well-formed XML is re-tokenized leniently (duplicate attributes keep
  the last value, run-together attributes, stray `<`, shapes missing `/>`, misnested or unclosed tags, text around
  the root, an `<svg>` left open inside a closed code fence; a reply cut off mid-SVG is not repaired) and must then
  pass the same strict sanitizer; a repair that would drop more than 10% of the shapes is refused.
  **Sanitize**: parse with xml.etree/defusedxml-like safe parsing, allowlist
  elements (svg,g,path,rect,circle,ellipse,line,polyline,polygon,text,tspan,defs,linearGradient,radialGradient,stop,
  clipPath,mask,title) and attributes (geometry/presentation/transform/id/class/font-*/text-anchor/
  dominant-baseline/letter-spacing/opacity/fill*/stroke*/offset/stop-*/gradient*/viewBox/xmlns/href only if starting
  with '#'); drop scripts, event handlers, foreignObject, external refs, style elements; enforce size limit 200 KB.
  Designs persisted in session.
- **orders.py:** price table (USD): base by material (cotton 12, organic 16, heavyweight 18, tri_blend 15, linen_blend 22,
  recycled_poly 14); print method (dtg 8, screen 6 + 30 setup/order, embroidery 12, puff 10); placement multiplier
  (front_and_back +7); sleeve long +5; XXL +3; quantity discount 10% at ≥10, 20% at ≥25. Round to cents.
  Validate email loosely. print.svg = the sanitized design SVG. At most 100 orders per session.
- **store.py:** SQLite tables `sessions(id, data json, updated_at)`, `orders(id, data json, created_at)`. IDs: `secrets.token_urlsafe(9)`.
  The order blob also keeps the creating session id and the SHA-256 of the requester token (hidden).

## 6. Frontend design language

- Bold, playful, poster-like. Off-white background `#FAF7F2`, ink `#141414`, accent hot red `#FF3B30`, secondary
  electric blue `#2F5BFF`. Display font: system stack heavy (e.g. "Inter", "Pretendard", system-ui) weight 800.
- Quiz screen: progress bar "Round 7 / 15" + depth chip; prompt large and centered; **two equal square cards**
  side-by-side (≥ 640 px) or stacked (mobile) with a circular **VS** badge between them. Card = image (1:1, cover),
  label (bold), sublabel (muted). Hover lift; click/tap or keys `A`/`←` and `B`/`→` to pick; chosen card pulses,
  other fades; then next question slides in. While waiting: skeleton cards. Image credit as tiny overlay text.
- Accessible: buttons are real `<button>`s, focus rings, alt text = label. Text meets WCAG AA contrast: small
  red text and small white-on-red text use the deeper red `#C8241B` (the hot red stays for large type, fills and
  red text on the ink bands). Long option labels step down a font size on both cards rather than being cut off.
- Studio: two-column (mockup left, controls right; stacked on mobile). `TeeMockup` draws a t-shirt silhouette SVG
  per fit & sleeve & neckline, fills shirt color, overlays design at placement (front_center large; left_chest small;
  back shows "BACK" view toggle). Design SVG injected via `<img src="data:image/svg+xml;utf8,...">` (never innerHTML)
  so scripts can't run even if sanitizer misses something.
- Copy is in English; tone fun but crisp.
