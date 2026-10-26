# Frontend contracts

Shared modules every page builds on. Field names mirror `backend/app/models.py` exactly (SPEC section 4).
If you need something that isn't here, add it in your own file rather than changing these signatures.

## Ground rules for page agents

- **Own your page.** Replace `src/pages/<Page>.tsx` (keep the **default export** and file name, since
  `App.tsx` imports them). Put page-only components in `src/components/<Name>.tsx` and page-only CSS in
  `src/pages/<Page>.css` (or `src/components/<Name>.css`), imported from your component. Use the tokens
  and utilities below, and **don't edit `styles.css`** (other agents share it).
- **Never inject HTML.** No `dangerouslySetInnerHTML`. Show design SVGs with
  `<img src={svgDataUrl(design.svg)} alt={design.title} />`.
- **External links** (Shop "Buy"): `target="_blank" rel="noopener noreferrer"`.
- **Accessibility:** real `<button>`s, visible focus (global `:focus-visible` ring is already set),
  `alt` text = option label / product title.
- **Errors:** catch, then `setError(errorMessage(err))`, then `<ErrorBanner message={error} onRetry={...} />`.
  `err.isUpstream` (502/504) means a retry makes sense. `err.isConflict` (409) usually means the
  user needs an earlier step, so navigate there.

## Routes (`App.tsx`)

| Path | Page (default export) | Guard |
|---|---|---|
| `/` | `pages/Landing.tsx` | none |
| `/quiz` | `pages/Quiz.tsx` | `RequireSession` |
| `/about-you` | `pages/AboutYou.tsx` | `RequireSession` |
| `/profile` | `pages/Profile.tsx` | `RequireSession` |
| `/shop` | `pages/Shop.tsx` | `RequireSession` |
| `/studio` | `pages/Studio.tsx` | `RequireSession` |
| `/order/:id` | `pages/Order.tsx` | none (shareable link, no session needed) |

`RequireSession` shows a loader while restoring, redirects to `/` if there is no valid session, and
shows a retry banner if the server is unreachable. Inside it, `useRequiredSession().session` is never null.
The layout (header with logo, `Stepper`, country chip and "Start over"; footer) wraps every page.
On `/order/:id` the stepper is hidden. Pages render inside `<main>`, so start with
`<section className="container section">`.

## `src/types.ts`

Types: `Depth`, `Choice`, `ContextKind`, `ContextStatus`, `Origin`, `Fit`, `Material`, `Neckline`,
`Sleeve`, `Size`, `PrintMethod`, `Placement`, `OrderStatus`, `Country`, `OptionImage`, `Option`,
`Question`, `Answer`, `ContextItem`, `Trait`, `TasteColor`, `Taste`, `Profile`, `Product`,
`ProductSpec`, `Design`, `PriceLine`, `Price`, `Customer`, `Order`, `AnsweredQuestion`,
`SessionState`, `Health`, `CreateSessionRequest`, `AnswerResult`, `ContextResponse`, `ContextUpload`,
`ShopResponse`, `DesignGenerateRequest`, `DesignResponse`, `CreateOrderRequest`.

Constants: `QUIZ_TOTAL = 15`; `DEFAULT_SPEC: ProductSpec` (same defaults as the backend:
white, regular, cotton, crew, short, M, dtg, front_center, qty 1, no notes).

Hex colors come back from the API normalized to lowercase `#rrggbb`. The backend accepts `#abc` too.

## `src/api.ts`

Every function returns a `Promise` and accepts an optional trailing `signal?: AbortSignal`.
All of them throw `ApiError` on any HTTP error or network failure.

```ts
class ApiError extends Error {
  readonly status: number;   // HTTP status; 0 = server unreachable
  readonly detail: string;   // backend's {"detail": string}, or a friendly fallback
  get isNotFound(): boolean; // 404
  get isConflict(): boolean; // 409
  get isUpstream(): boolean; // 502 | 504 (AI / web search failed: offer "Try again")
}
errorMessage(err: unknown): string     // user-facing message for anything thrown
isAbortError(err: unknown): boolean    // true for AbortController cancellations (ignore these)

getHealth(): Promise<Health>                                          // GET  /api/health
createSession(country: Country): Promise<SessionState>                // POST /api/session
getSession(sessionId): Promise<SessionState>                          // GET  /api/session/{id}  (404 unknown)
deleteSession(sessionId): Promise<void>                               // DELETE /api/session/{id} (extra; "Start over"; orders stay)
warmQuiz(country: Country): Promise<void>                             // POST /api/quiz/warm (extra; pre-generates round 1; 202)
startQuiz(sessionId): Promise<Question>                               // POST /api/quiz/{id}/start  (idempotent; 409 if done)
answerQuiz(sessionId, answer: Answer): Promise<AnswerResult>          // POST /api/quiz/{id}/answer (409 stale question_id)
addContext(sessionId, upload: ContextUpload): Promise<ContextResponse>// POST /api/context/{id}  multipart; returns ALL items
removeContext(sessionId, itemId): Promise<ContextResponse>            // DELETE /api/context/{id}/{item_id}
buildProfile(sessionId): Promise<Profile>                             // POST /api/profile/{id} (409 quiz not done; regenerates)
suggestProducts(sessionId): Promise<ShopResponse>                     // POST /api/shop/{id}/suggest (409 no profile; up to ~75 s)
generateDesigns(sessionId, req: DesignGenerateRequest): Promise<DesignResponse> // POST /api/design/{id}/generate; returns NEW designs only
quotePrice(spec: ProductSpec): Promise<Price>                         // POST /api/quote (extra endpoint: live Studio price)
createOrder(req: CreateOrderRequest): Promise<Order>                  // POST /api/orders
getOrder(orderId): Promise<Order>                                     // GET  /api/orders/{id} (public view: email masked, address "(hidden)")
requestOrder(orderId, customer: Customer): Promise<Order>             // POST /api/orders/{id}/request (status -> "requested"; 422 bad email;
                                                                      //   stores the X-Order-Token it gets; 403 when someone else requested it)
isOrderRequester(orderId): boolean  // this browser placed the request (holds its token) and may update it
printSvgUrl(orderId): string        // href for <a download> of the print-ready SVG
svgDataUrl(svg: string): string     // data:image/svg+xml;utf8,... for <img src>
```

`ContextUpload = { files?: File[]; urls?: string[]; text?: string | null }`. Limits per session:
5 files (pdf, docx, txt, md; 10 MB each), 5 URLs, 5 notes of up to 20,000 characters each. Over the
limit returns 422 (413 for an oversized file or body). Costly endpoints (session, warm, context,
profile, shop, design, orders) answer 429 with Retry-After when one client sends too many; the
message is in `detail` like any other error.

## `src/SessionContext.tsx`

```ts
<SessionProvider>              // already mounted in main.tsx (inside <BrowserRouter>)
useSession(): SessionContextValue
useRequiredSession(): SessionContextValue & { session: SessionState }   // only inside <RequireSession>
<RequireSession>{children}</RequireSession>                             // used by App.tsx

interface SessionContextValue {
  session: SessionState | null;
  status: "loading" | "ready" | "none" | "error";
  error: string | null;                                   // set when status === "error"
  setSession(s: SessionState): void;                      // replace + persist id
  updateSession(fn: (s: SessionState) => SessionState): void;  // local functional update
  refresh(): Promise<SessionState | null>;                // re-GET /api/session/{id}; null if gone (id cleared)
  startSession(country: Country): Promise<SessionState>;  // POST /api/session + store id
  reset(): void;                                          // DELETE the session (best effort) + forget it and the shortlist; then navigate("/")
}
```

Keep the shared state current after mutations so the stepper and other pages see them. Either
call `refresh()`, or patch locally:

```ts
updateSession((s) => ({ ...s, current_question: q }));                        // after startQuiz / answerQuiz
updateSession((s) => ({ ...s, context: res.items }));                         // after addContext / removeContext
updateSession((s) => ({ ...s, profile }));                                    // after buildProfile
updateSession((s) => ({ ...s, designs: [...s.designs, ...res.designs] }));    // after generateDesigns
```

After the last quiz answer (`done: true`), call `await refresh()` before navigating so `quiz_done` is true.

## `src/session.ts` (localStorage)

```ts
SESSION_KEY = "tee.session"; SHORTLIST_KEY = "tee.shortlist"; ORDER_TOKENS_KEY = "tee.orderTokens"
getSessionId(): string | null; setSessionId(id): void; clearSessionId(): void
getOrderToken(orderId): string | null; setOrderToken(orderId, token): void   // requester tokens (kept across "Start over")
clearSessionScopedStorage(): void              // drops "tee.shop", "tee.studio", "tee.profileStale.*" (reset() calls it)
getShortlist(): Product[]                      // keyed by product.url
isShortlisted(p: Pick<Product, "url">): boolean
toggleShortlist(p: Product): Product[]         // add/remove; returns new list
removeFromShortlist(url: string): Product[]
clearShortlist(): Product[]
subscribeShortlist(cb: (items: Product[]) => void): () => void   // this tab + other tabs
useShortlist(): Product[]                      // React hook, live
```

## `src/countries.ts`

```ts
interface CountryInfo extends Country { flag: string; aliases: string[] }
COUNTRIES: CountryInfo[]                       // all 249 ISO 3166-1 alpha-2, sorted by name
flagEmoji(code: string): string                // "KR" -> 🇰🇷 (Windows gets a bundled flag font via main.tsx)
findCountry(code): CountryInfo | undefined
toCountry(c: CountryInfo): Country             // { code, name } for the API
searchCountries(query: string, limit?): CountryInfo[]   // ranked: code, name, aliases ("Korea", "UK", "日本"), accent-insensitive
guessCountry(): CountryInfo | undefined        // from browser locale
```

## `src/components/`

```tsx
<Stepper session={SessionState | null} />                  // in the header already; buildSteps(session) exported too
<Spinner size?={28} label?="Loading" className? />         // inline ring, role="status"
<PageLoader label?="Loading…" />                           // centered spinner + caption
<ErrorBanner message onRetry? onDismiss? className? />     // role="alert"
<PagePlaceholder eyebrow title>{children}</PagePlaceholder> // scaffold-only; drop once your page is real
```

## `src/styles.css`: tokens and utility classes

**Tokens (CSS variables):**
- Colors: `--bg` #FAF7F2, `--bg-2`, `--surface`, `--ink` #141414, `--ink-2`, `--muted`, `--line`,
  `--accent` #FF3B30 (`--accent-ink`, `--accent-soft`), `--blue` #2F5BFF (`--blue-ink`, `--blue-soft`),
  `--success(-soft)`, `--warning(-soft)`, `--danger(-soft)`.
- Type: `--font-display`, `--font-body`, `--font-mono`, `--weight-display` (800); `--text-xs` through
  `--text-3xl` and `--text-hero`.
- Spacing (4 px scale): `--space-1` (4) to `--space-8` (64).
- Shape and depth: `--radius-sm | --radius | --radius-lg | --radius-pill`, `--border` (2 px ink),
  `--shadow-sm | --shadow | --shadow-lg`, poster offset `--shadow-pop | --shadow-pop-lg`, `--focus-ring`.
- Motion: `--ease-out`, `--ease-pop`, `--dur-fast | --dur | --dur-slow`.
- Layout: `--container` (1120 px), `--container-narrow` (720 px), `--header-h`.

**Classes:**

| Group | Classes |
|---|---|
| Layout | `.container`, `.container-narrow` (add to `.container`), `.section`, `.stack`, `.stack-sm`, `.stack-lg`, `.row`, `.row-between`, `.grid` (auto-fill 240 px), `.grid-2` (2 cols ≥ 900 px), `.center` |
| Text | `.display`, `.hero-title`, `.eyebrow`, `.lead`, `.muted`, `.small`, `.tiny`, `.mono`, `.accent`, `.blue`, `.visually-hidden` |
| Buttons | `.btn` + `.btn-primary` (red), `.btn-blue`, `.btn-dark`, `.btn-ghost`; sizes `.btn-sm`, `.btn-lg`; `.btn-block`; `.icon-btn` (round 40 px) |
| Cards | `.card` (poster border + offset shadow), `.card-flat`, `.card-hover` (lift on hover) |
| Chips/badges | `.chip` + `.chip-accent`, `.chip-blue`, `.chip-dark`, `.chip-soft`; `button.chip[aria-pressed="true"]` = selected; status `.badge-ok`, `.badge-limited`, `.badge-error` (combine with `.chip`) |
| Misc | `.swatch` (40 px color circle; set `background`), `.divider` |
| Forms | `.field` (label+control column), `.label`, `.hint`, `.input`, `.select`, `.textarea`; `aria-invalid="true"` turns the border red |
| Loading | `.skeleton` (shimmer block) + `.skeleton-text` / `.skeleton-square` (1:1); `.spinner`, `.page-loader` |
| Feedback | `.error-banner` (used by `ErrorBanner`) |
| Motion | `.fade-in`, `.slide-up`, `.pop-in` (honors `prefers-reduced-motion`) |

Shell classes (`.app*`, `.logo*`, `.stepper*`, `.placeholder`) belong to the layout and shouldn't be reused.
