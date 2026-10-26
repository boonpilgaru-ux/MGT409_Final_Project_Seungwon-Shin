import { Fragment, useEffect, useMemo, useRef, useState, type ButtonHTMLAttributes, type FormEvent, type ReactNode } from "react";
import { Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  addContext, answerQuiz, buildProfile, createOrder, errorMessage, getOrder, removeContext,
  getUsage, isOrderRequester, printSvgUrl, quotePrice, requestOrder, startQuiz, svgDataUrl, warmQuiz,
} from "./api";
import { useRequiredSession, useSession } from "./SessionContext";
import { removeFromShortlist, toggleShortlist, useShortlist } from "./session";
import { DEFAULT_SPEC, type Country, type Customer, type Design, type Product, type ProductSpec, type Question, type UsageSummary } from "./types";
import { TeeMockup } from "./components/commerce/TeeMockup";
import { markStudioOpened, mergeDesigns, pendingDesignJob, prefetchDesigns, prefetchIsStale, startDesignJob } from "./components/commerce/designJobs";
import { prefetchShop, profileKey, readShopCache, startSearch } from "./components/commerce/shopSearch";

const MARKETS: Country[] = [
  { code: "US", name: "United States" }, { code: "KR", name: "South Korea" },
  { code: "JP", name: "Japan" }, { code: "CN", name: "China" }, { code: "IN", name: "India" },
];
const SAMPLE_CHOICES: Record<string, { prompt: string; a: string; b: string }> = {
  US: { prompt: "Pick your road-trip fuel", a: "Salt & vinegar chips", b: "Mystery gas-station candy" },
  KR: { prompt: "Choose a perfect late-night snack", a: "Tteokbokki, extra spicy", b: "Hotteok on the walk home" },
  JP: { prompt: "Choose one world to join", a: "Slam Dunk's basketball team", b: "Dragon Ball's Capsule Corp." },
  CN: { prompt: "Settle the weekend dinner debate", a: "Hot pot with the whole crew", b: "A long dim sum lunch" },
  IN: { prompt: "How should the festival end?", a: "One more garba round", b: "Chai and gossip on the curb" },
};
const cx = (...items: Array<string | false | undefined>) => items.filter(Boolean).join(" ");

function Button({ children, className, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={cx("button", className)} {...props}>{children}</button>;
}
function Eyebrow({ children }: { children: ReactNode }) { return <div className="eyebrow">{children}</div>; }
function Alert({ children }: { children: ReactNode }) { return children ? <div className="alert" role="alert">{children}</div> : null; }
function Page({ children, wide = false }: { children: ReactNode; wide?: boolean }) { return <section className={cx("page", wide && "page-wide")}>{children}</section>; }

function UsagePanel({ sessionId }: { sessionId: string }) {
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => void getUsage(sessionId).then((next) => { if (live) setUsage(next); }).catch(() => undefined);
    load();
    const timer = window.setInterval(load, 3000);
    return () => { live = false; window.clearInterval(timer); };
  }, [sessionId]);
  if (!usage || !usage.request_count) return null;
  return <details className="usage-panel"><summary>Model usage and estimated cost</summary><dl>
    <div><dt>Model requests</dt><dd>{usage.request_count.toLocaleString()}</dd></div>
    <div><dt>Input tokens</dt><dd>{usage.input_tokens.toLocaleString()}</dd></div>
    <div><dt>Cached input tokens</dt><dd>{usage.cached_input_tokens.toLocaleString()}</dd></div>
    <div><dt>Output tokens</dt><dd>{usage.output_tokens.toLocaleString()}</dd></div>
    <div className="usage-total"><dt>Estimated model cost</dt><dd>${usage.estimated_cost_usd.toFixed(6)}</dd></div>
  </dl><p>Estimate uses the course Luna token rates. Hosted web-search fees are not included.</p></details>;
}

function Shell() {
  const { session, reset } = useSession();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [menu, setMenu] = useState(false);
  const restart = () => {
    if (window.confirm("Start a new session? Your answers, uploads, and saved shirts will be cleared. Existing shareable order links will keep working.")) {
      reset(); navigate("/");
    }
  };
  const links = [
    ["01", "Choose a country", "/"], ["02", "The balance game", "/quiz"],
    ["03", "A little more you", "/about-you"], ["04", "Your identity", "/profile"],
    ["05", "Find a shirt", "/shop"], ["06", "Design a shirt", "/studio"],
  ];
  const isBlocked = (to: string) => {
    if (to === "/") return false;
    if (!session) return true;
    if (to === "/quiz") return session.quiz_done;
    if (to === "/about-you" || to === "/profile") return !session.quiz_done;
    if (to === "/shop" || to === "/studio") return !session.profile;
    return false;
  };
  return <div className="app-shell">
    <aside className={cx("rail", menu && "rail-open")}>
      <Link to="/" className="wordmark" aria-label="Tee:dentity home">tee<span>:</span>dentity</Link>
      <div className="rail-caption">A PERSONAL STYLE STUDY</div>
      <nav className="rail-nav" aria-label="Your journey">
        {links.map(([n, label, to]) => <Link key={to} to={to} aria-current={pathname === to ? "step" : undefined} aria-disabled={isBlocked(to)} title={isBlocked(to) ? "Finish the previous step first" : undefined} onClick={(e) => { setMenu(false); if (isBlocked(to)) e.preventDefault(); }} className={cx("rail-link", pathname === to && "is-current", isBlocked(to) && "is-disabled")}>
          <span className="rail-number">{n}</span><span>{label}</span>{to === "/profile" && session?.profile && <i className="rail-dot" />}
        </Link>)}
      </nav>
      <div className="rail-bottom">
        <div className="rail-note"><span className="status-light" /> Designed around what makes you, you.</div>
        <div className="rail-foot">A small experiment in self-expression.<br />Demo checkout · no charge</div>
      </div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <button className="mobile-menu" onClick={() => setMenu(!menu)} aria-label="Toggle menu">☰</button>
        <span className="topbar-label">YOUR PERSONAL STYLE STUDY</span>
        <div className="topbar-right">
          {session && <span className="country-pill"><span className="status-light" />{session.country.name}</span>}
          {session?.quiz_done && pathname !== "/profile" && <Link className="top-profile-link" to="/profile">My profile <span>↗</span></Link>}
          {session && <button className="quiet-link" onClick={restart}>Start over ↗</button>}
        </div>
      </header>
      <main id="main"><Outlet /></main>
      <footer className="page-footer"><span>TEE:DENTITY / A STUDENT PROJECT</span><span>Find a shirt that feels like yours.</span></footer>
    </div>
  </div>;
}

function Landing() {
  const { session, status, startSession } = useSession();
  const [market, setMarket] = useState("US");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const navigate = useNavigate();
  const sample = SAMPLE_CHOICES[market];
  if (status === "loading") return <Page><div className="loading-panel">Restoring your saved session…</div></Page>;
  if (session) return <Navigate to={session.quiz_done ? "/profile" : "/quiz"} replace />;
  const begin = async () => {
    const country = MARKETS.find((m) => m.code === market)!;
    setBusy(true); setError("");
    try {
      await warmQuiz(country).catch(() => undefined);
      await startSession(country); navigate("/quiz");
    } catch (e) { setError(errorMessage(e)); setBusy(false); }
  };
  return <Page wide><div className="landing-grid">
    <div className="landing-copy">
      <Eyebrow><span className="eyebrow-mark">✳</span> YOUR TASTE, IN TEN CHOICES</Eyebrow>
      <h1>Find a tee<br /><em>that feels like you.</em></h1>
      <p className="landing-lede">Choose a country, answer 10 quick A-or-B questions, then get a personal style profile, real shirt ideas, and a custom design.</p>
      <div className="country-picker"><label htmlFor="country">FIRST, PICK YOUR CULTURAL HOME</label>
        <div className="select-wrap"><select id="country" value={market} onChange={(e) => setMarket(e.target.value)}>
          {MARKETS.map((m) => <option key={m.code} value={m.code}>{m.name}</option>)}
        </select><span>⌄</span></div>
        <p className="support-note">More places are on the way. This is just our first five.</p>
      </div>
      <Alert>{error}</Alert>
      <Button className="button-dark button-large" onClick={() => void begin()} disabled={busy}>{busy ? "Setting up your game…" : <>Start the 10-question game <span>↗</span></>}</Button>
      <div className="tiny-proof"><span>About 3 minutes</span><b>·</b><span>No wrong answers</span><b>·</b><span>Optional uploads</span></div>
    </div>
    <div className="landing-art" aria-label="Example of a country-aware A-or-B question">
      <div className="art-top"><span>EXAMPLE QUESTION</span><span>{MARKETS.find((m) => m.code === market)?.name.toUpperCase()}</span></div>
      <div className="sample-prompt">{sample.prompt}</div>
      <div className="art-card art-card-a"><div className="art-symbol">A</div><div><small>OPTION A</small><strong>{sample.a}</strong><span>Pick the one you would actually choose</span></div></div>
      <div className="art-or">OR</div>
      <div className="art-card art-card-b"><div className="art-symbol">B</div><div><small>OPTION B</small><strong>{sample.b}</strong><span>Both answers are good answers</span></div></div>
      <div className="art-bottom"><span>THE REAL GAME HAS 10 CHOICES</span><span>01—10</span></div>
    </div>
  </div>
    <div className="how-grid"><div><Eyebrow>THE SHORT VERSION</Eyebrow><h2>From “that’s so me”<br />to actually wearing it.</h2></div>
      <div className="how-step"><b>01</b><strong>Answer 10 choices</strong><p>Pick between two local foods, stories, and everyday moods.</p></div>
      <div className="how-step"><b>02</b><strong>See your style profile</strong><p>Your picks shape a short, personal T-shirt brief.</p></div>
      <div className="how-step"><b>03</b><strong>Find or design a tee</strong><p>Browse real shirts or make a custom design and shareable order link.</p></div>
    </div>
  </Page>;
}

function OptionTile({ option, side, onClick, disabled }: { option: Question["option_a"]; side: string; onClick: () => void; disabled: boolean }) {
  const [imageFailed, setImageFailed] = useState(false);
  const showPhoto = Boolean(option.image && !imageFailed);
  return <button onClick={onClick} disabled={disabled} className="choice-tile">
    <div className="choice-picture" style={{ background: showPhoto ? "#e9e9df" : `linear-gradient(145deg, ${option.color}, #f2ead4)` }}>
      {showPhoto ? <img src={option.image!.url} alt={option.label} onError={() => setImageFailed(true)} /> : <span className="choice-emoji">{option.emoji}</span>}
      <span className="choice-index">{side}</span>
      {showPhoto && option.image?.credit && <span className="photo-credit">Photo: {option.image.credit}</span>}
    </div>
    <div className="choice-label"><strong>{option.label}</strong>{option.sublabel && <span>{option.sublabel}</span>}</div>
  </button>;
}

function Quiz() {
  const { session, setSession } = useRequiredSession();
  const navigate = useNavigate();
  const [question, setQuestion] = useState<Question | null>(session.current_question);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { if (!question && !session.quiz_done) void startQuiz(session.id).then(setQuestion).catch((e) => setError(errorMessage(e))); }, [question, session.id, session.quiz_done]);
  useEffect(() => { if (session.quiz_done) navigate("/about-you", { replace: true }); }, [session.quiz_done, navigate]);
  const choose = async (choice: "A" | "B") => {
    if (!question || busy) return;
    setBusy(true); setError("");
    try {
      const answer = { question_id: question.id, choice };
      const result = await answerQuiz(session.id, answer);
      setSession({ ...session, answers: [...session.answers, { question, choice }], current_question: result.next, quiz_done: result.done });
      if (result.done) { setBusy(false); navigate("/about-you"); }
      else { setQuestion(result.next); setBusy(false); }
    } catch (e) { setError(errorMessage(e)); setBusy(false); }
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement)?.closest("input,textarea,select,[contenteditable='true']")) return;
      if (event.key === "ArrowLeft" || event.key.toLowerCase() === "a") { event.preventDefault(); void choose("A"); }
      if (event.key === "ArrowRight" || event.key.toLowerCase() === "b") { event.preventDefault(); void choose("B"); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [question, busy]);
  if (!question) return <Page><div className="loading-panel"><span className="loader-dot" />Building your first choice…</div><Alert>{error}</Alert>{error && <Button className="button-outline" onClick={() => { setError(""); void startQuiz(session.id).then(setQuestion).catch((e) => setError(errorMessage(e))); }}>Try again ↻</Button>}</Page>;
  return <Page><div className="quiz-head"><Eyebrow>THE BALANCE GAME · {session.country.name.toUpperCase()}</Eyebrow><div className="quiz-count"><span>ROUND</span><strong>{String(question.index).padStart(2, "0")}</strong><i>/ {question.total}</i></div></div>
    <div className="progress-track" role="progressbar" aria-label="Quiz progress" aria-valuemin={0} aria-valuemax={question.total} aria-valuenow={question.index}><span style={{ width: `${(question.index / question.total) * 100}%` }} /></div>
    <div className="quiz-title"><span className="depth-tag">QUICK CHOICE</span><h1>{question.prompt}</h1><p>Choose the one you like more. There’s no wrong answer.</p></div>
    <div className="choices"><OptionTile key={`${question.id}-a`} option={question.option_a} side="A" onClick={() => void choose("A")} disabled={busy} /><div className="versus"><span>OR</span></div><OptionTile key={`${question.id}-b`} option={question.option_b} side="B" onClick={() => void choose("B")} disabled={busy} /></div>
    <div className="quiz-foot"><span>{busy ? "Saving your choice…" : "Tap either card to answer"}</span><span>Keys A / B or ← / → work too</span></div><Alert>{error}</Alert>
  </Page>;
}

function AboutYou() {
  const { session, updateSession } = useRequiredSession();
  const [text, setText] = useState(""); const [urls, setUrls] = useState(""); const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const navigate = useNavigate();
  const remainingFiles = Math.max(0, 5 - session.context.filter((item) => item.kind === "file").length);
  const remainingUrls = Math.max(0, 5 - session.context.filter((item) => item.kind === "url").length);
  const remove = async (itemId: string) => {
    setError("");
    try { const response = await removeContext(session.id, itemId); updateSession((s) => ({ ...s, context: response.items })); }
    catch (err) { setError(errorMessage(err)); }
  };
  const save = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError(""); setNotice("");
    try {
      if (text.trim() || urls.trim() || files.length) {
        const cleanUrls = [...new Set(urls.split(/[\s,]+/).filter(Boolean))];
        if (cleanUrls.length > remainingUrls) throw new Error(`You can add ${remainingUrls} more link${remainingUrls === 1 ? "" : "s"}.`);
        if (text.trim() && session.context.filter((item) => item.kind === "text").length >= 5) throw new Error("You already have five notes. Remove one before adding another.");
        const response = await addContext(session.id, { text, urls: cleanUrls, files });
        updateSession((s) => ({ ...s, context: response.items })); setNotice("Added to your style brief.");
      }
      navigate("/profile");
    } catch (err) { setError(errorMessage(err)); setBusy(false); }
  };
  return <Page><div className="page-heading"><Eyebrow>OPTIONAL · ADD YOUR OWN CONTEXT</Eyebrow><h1>Anything else<br />we should know?</h1><p>Your answers are enough. If you want, add a note, a public link, or a file to make your profile more personal.</p></div>
    <div className="context-layout"><form className="context-form" onSubmit={(e) => void save(e)}>
      <label className="field-label" htmlFor="context-text">A note <span>UP TO 5 NOTES · 5,000 CHARACTERS EACH</span></label><textarea id="context-text" rows={4} maxLength={5000} placeholder="I love old movie posters, spicy food, and shirts that are simple but a little weird…" value={text} onChange={(e) => setText(e.target.value)} />
      <label className="field-label" htmlFor="context-url">Public links <span>{remainingUrls} OF 5 LINKS LEFT · SEPARATE WITH A SPACE OR NEW LINE</span></label><textarea id="context-url" rows={2} placeholder="Portfolio, public post, or social profile URL" value={urls} onChange={(e) => setUrls(e.target.value)} />
      <label className="field-label" htmlFor="context-file">Files <span>{remainingFiles} OF 5 FILES LEFT · PDF, DOCX, TXT OR MD · 10 MB MAX EACH</span></label><input id="context-file" type="file" multiple accept=".pdf,.docx,.txt,.md" disabled={remainingFiles === 0} onChange={(e) => {
        const chosen = Array.from(e.target.files ?? []);
        const tooLarge = chosen.find((file) => file.size > 10 * 1024 * 1024);
        if (tooLarge) { setError(`${tooLarge.name} is over the 10 MB limit.`); setFiles([]); e.currentTarget.value = ""; }
        else if (chosen.length > remainingFiles) { setError(`You can add ${remainingFiles} more file${remainingFiles === 1 ? "" : "s"}.`); setFiles([]); e.currentTarget.value = ""; }
        else { setError(""); setFiles(chosen); }
      }} />
      {files.length > 0 && <div className="selected-files">{files.map((file) => <span key={`${file.name}-${file.size}`}>{file.name} <i>{(file.size / 1024 / 1024).toFixed(1)} MB</i></span>)}</div>}
      <div className="privacy-note"><span>↗</span><p>Only share information you are comfortable using to shape your style profile. This step is optional.</p></div>
      {session.context.length > 0 && <div className="context-items"><h3>Already in your profile brief</h3>{session.context.map((item) => <div className="context-item" key={item.id}><div><strong>{item.name}</strong><p>{item.summary || item.error || `${item.chars.toLocaleString()} characters added`}</p><span className={cx("context-status", item.status)}>{item.status}</span></div><button type="button" onClick={() => void remove(item.id)} aria-label={`Remove ${item.name}`}>Remove</button></div>)}</div>}
      <Alert>{error}</Alert><Alert>{notice}</Alert>
      <div className="action-row"><button type="button" className="quiet-link" onClick={() => navigate("/profile")}>Skip and see my profile</button><Button className="button-dark" disabled={busy}>{busy ? "Adding your details…" : "Continue to my profile ↗"}</Button></div>
    </form><aside className="context-side"><span className="context-side-mark">“</span><h3>The quiz is enough to get started.</h3><p>Extra context can make your profile more specific, but you can leave every field blank and continue.</p><div className="context-limit">YOUR PROFILE USES YOUR TEN ANSWERS</div></aside></div>
  </Page>;
}

function Profile() {
  const { session, setSession, updateSession } = useRequiredSession();
  const [busy, setBusy] = useState(!session.profile); const [error, setError] = useState("");
  const profileRequested = useRef(false);
  const startBackgroundWork = (profile: NonNullable<typeof session.profile>) => {
    const key = profileKey(profile);
    prefetchShop(session.id, key);
    prefetchDesigns(session.id, key, session.designs.length > 0, (fresh) => updateSession((s) => s.id === session.id ? { ...s, designs: mergeDesigns(s.designs, fresh) } : s));
  };
  useEffect(() => {
    if (session.profile) { startBackgroundWork(session.profile); setBusy(false); return; }
    if (!profileRequested.current) {
      profileRequested.current = true;
      void buildProfile(session.id).then((p) => { setSession({ ...session, profile: p }); startBackgroundWork(p); setBusy(false); }).catch((e) => { setError(errorMessage(e)); setBusy(false); });
    }
  }, []);
  const p = session.profile;
  if (busy) return <Page><div className="loading-panel"><span className="loader-dot" />Reading between your choices…</div></Page>;
  if (!p) return <Page><Alert>{error}</Alert><Button onClick={() => window.location.reload()}>Try again ↻</Button></Page>;
  return <Page wide>
    <div className="profile-actions" aria-label="Choose what to do next">
      <Link className="profile-action" to="/shop"><span className="profile-action-icon">⌕</span><span><small>BASED ON YOUR ANSWERS</small><strong>Find a T-shirt</strong><i>See real, buyable shirts that fit your taste.</i></span><b>↗</b></Link>
      <Link className="profile-action profile-action-design" to="/studio"><span className="profile-action-icon">✳</span><span><small>MADE FROM YOUR TEE BRIEF</small><strong>Design your own</strong><i>Choose a graphic, garment, fit, and print details.</i></span><b>↗</b></Link>
    </div>
    <div className="profile-hero"><div className="profile-info"><Eyebrow>YOUR STYLE PROFILE · {session.country.name.toUpperCase()}</Eyebrow><div className="profile-mark">T:</div><h1>{p.archetype}</h1><h2>{p.tagline}</h2><p>{p.summary}</p><div className="profile-roots"><span>CULTURAL REFERENCES IN YOUR ANSWERS</span>{p.cultural_roots}</div></div>
      <aside className="brief-card"><div className="brief-card-top"><span>THE TEE BRIEF</span><span>✳</span></div><p>{p.tshirt_brief}</p><div className="brief-colors">{p.taste.colors.map((c) => <span key={c.hex} title={c.name} style={{ background: c.hex }} />)}</div><div className="brief-tags">{[...p.taste.aesthetics, ...p.taste.motifs].slice(0, 6).map((x) => <span key={x}>{x}</span>)}</div><div className="brief-type">TYPE FEEL <b>{p.taste.typography}</b></div></aside></div>
    <details className="answer-recap"><summary>See the 10 choices behind this profile</summary><div className="answer-list">{session.answers.map((answer, i) => { const option = answer.choice === "A" ? answer.question.option_a : answer.question.option_b; return <div className="answer-row" key={answer.question.id}><span>{String(i + 1).padStart(2, "0")}</span><p>{answer.question.prompt}</p><strong>{option.label}</strong></div>; })}</div></details>
    <div className="profile-lower"><div><Eyebrow>WHAT YOUR ANSWERS SUGGEST</Eyebrow><div className="traits">{p.traits.map((t) => <div className="trait" key={t.name}><div className="trait-top"><strong>{t.name}</strong><span>{t.high_label}</span></div><div className="trait-track"><i style={{ width: `${t.score}%` }} /></div><p>{t.description}</p></div>)}</div></div>
      <div className="profile-values"><Eyebrow>THE VALUES THAT CAME THROUGH</Eyebrow><p>These themes shaped your style profile and shirt brief.</p><div className="value-tags">{p.values.map((v) => <span key={v}>{v}</span>)}</div><div className="profile-colors-label">PALETTE IDEAS</div><div className="brief-colors">{p.taste.colors.map((c) => <span key={c.hex} title={c.name} style={{ background: c.hex }} />)}</div><div className="brief-tags">{p.taste.fits.map((x) => <span key={x}>{x}</span>)}</div></div></div>
    <UsagePanel sessionId={session.id} />
  </Page>;
}

function Shop() {
  const { session } = useRequiredSession();
  const currentProfile = session.profile!;
  const currentProfileKey = profileKey(currentProfile);
  const [products, setProducts] = useState<Product[]>(() => readShopCache(session.id, currentProfileKey)?.products ?? []);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const shortlist = useShortlist();
  const [showSaved, setShowSaved] = useState(false);
  const firstSearch = useRef(false);
  useEffect(() => {
    if (!showSaved) return;
    const onEscape = (event: KeyboardEvent) => { if (event.key === "Escape") setShowSaved(false); };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onEscape);
    return () => { document.body.style.overflow = ""; window.removeEventListener("keydown", onEscape); };
  }, [showSaved]);
  const search = async (force = false) => {
    if (!force) { const cached = readShopCache(session.id, currentProfileKey); if (cached) { setProducts(cached.products); return; } }
    setBusy(true); setError("");
    try { const result = await startSearch(session.id, currentProfileKey).promise; setProducts(result.products); }
    catch (e) { setError(errorMessage(e)); }
    finally { setBusy(false); }
  };
  useEffect(() => { if (!products.length && !firstSearch.current) { firstSearch.current = true; void search(); } }, []);
  const saved = useMemo(() => new Set(shortlist.map((p) => p.url)), [shortlist]);
  return <Page wide><div className="shop-head"><div><Eyebrow>REAL SHIRTS · BASED ON YOUR ANSWERS</Eyebrow><h1>Find a T-shirt.</h1><p>Each pick is matched to your profile and tastes. We show the exact answer match when product details support one.</p></div><div className="shop-actions"><Button className="button-outline" onClick={() => setShowSaved(true)}>♡ Saved shirts ({shortlist.length})</Button><Button className="button-outline" disabled={busy} onClick={() => void search(true)}>{busy ? "Searching…" : "Refresh suggestions ↻"}</Button></div></div>
    <div className="shop-explainer"><span>WHAT YOU’LL SEE</span><p>Real product links from retailers. “Answer match” means the selected item appears on the product page or in its search title; “Why it fits” explains the style connection.</p></div>
    <Alert>{error}</Alert>{busy && <div className="loading-panel"><span className="loader-dot" />Searching real shops for your style… This can take a little over a minute.</div>}
    {!busy && products.length === 0 && <div className="empty-panel">No shirts came back this time. Try a fresh search in a moment.</div>}
    <div className="product-grid">{products.map((p, i) => <article className="product-card" key={p.id}><div className="product-image">{p.image_url && <img src={p.image_url} alt={p.title} loading="lazy" onError={(e) => { e.currentTarget.style.display = "none"; }} />}<span className="product-placeholder" aria-hidden="true">tee<span>:</span></span><span className="product-num">{String(i + 1).padStart(2, "0")}</span><button className={cx("save-button", saved.has(p.url) && "is-saved")} onClick={() => toggleShortlist(p)} aria-pressed={saved.has(p.url)} aria-label={saved.has(p.url) ? "Remove from saved shirts" : "Save shirt"}>{saved.has(p.url) ? "♥" : "♡"}</button></div><div className="product-meta"><span>{p.retailer || p.brand} · {p.origin === "local" ? "Local" : p.origin === "indie" ? "Independent" : "Global"}</span><b>{p.price ?? "Price on retailer site"}</b></div><h3>{p.title}</h3>{p.pick_match && <div className="pick-match"><b>ANSWER MATCH</b><span>“{p.pick_match}”</span><small>{p.pick_match_source === "product_page" ? "Found on product page" : "Found in search title"}</small></div>}<div className="why-match"><b>WHY IT FITS</b><p>{p.why}</p></div><a className="product-link" href={p.url} target="_blank" rel="noopener noreferrer">View at {p.retailer || "retailer"} <span>↗</span></a></article>)}</div>
    <div className="shortlist-note">Saved shirts stay on this device, so you can compare them later.</div>
    {showSaved && <div className="drawer-backdrop" onClick={() => setShowSaved(false)}><aside className="saved-drawer" role="dialog" aria-modal="true" aria-label="Saved shirts" onClick={(e) => e.stopPropagation()}><div className="saved-drawer-head"><div><Eyebrow>YOUR SHORTLIST</Eyebrow><h2>Saved shirts <span>{shortlist.length}</span></h2></div><button className="drawer-close" onClick={() => setShowSaved(false)} aria-label="Close saved shirts">×</button></div>{shortlist.length ? <div className="saved-list">{shortlist.map((p) => <div className="saved-row" key={p.url}>{p.image_url ? <img src={p.image_url} alt="" /> : <div className="saved-thumb">tee:</div>}<div className="saved-info"><b>{p.title}</b><span>{p.retailer} · {p.price || "See price"}</span><a href={p.url} target="_blank" rel="noopener noreferrer">Open product ↗</a></div><button className="saved-remove" onClick={() => removeFromShortlist(p.url)} aria-label={`Remove ${p.title}`}>Remove</button></div>)}</div> : <p className="saved-empty">Tap ♡ on a shirt to save it here.</p>}</aside></div>}
  </Page>;
}

function Studio() {
  const { session, updateSession } = useRequiredSession();
  const [designs, setDesigns] = useState<Design[]>(session.designs ?? []); const [chosen, setChosen] = useState<Design | null>(session.designs?.[0] ?? null);
  const [feedback, setFeedback] = useState(""); const [spec, setSpec] = useState<ProductSpec>(() => session.designs?.[0]?.recommended_spec ?? DEFAULT_SPEC); const [price, setPrice] = useState<{ unit: number; total: number } | null>(null);
  const [busy, setBusy] = useState(!session.designs?.length || Boolean(pendingDesignJob(session.id))); const [error, setError] = useState(""); const [placing, setPlacing] = useState(false); const navigate = useNavigate();
  const firstGeneration = useRef(false);
  const currentProfileKey = profileKey(session.profile!);
  const gen = async (note: string | null = null, base: Design | null = null): Promise<boolean> => {
    setBusy(true); setError("");
    try {
      const job = startDesignJob(session.id, base ? "revise" : session.designs.length ? "more" : "initial", { feedback: note, base_design_id: base?.id ?? null, count: base ? 1 : 3 }, (fresh) => updateSession((s) => s.id === session.id ? { ...s, designs: mergeDesigns(s.designs, fresh) } : s));
      const fresh = await job.promise;
      setDesigns((current) => mergeDesigns(current, fresh));
      if (fresh[0] && (base || !chosen)) { setChosen(fresh[0]); setSpec(fresh[0].recommended_spec); }
      return true;
    } catch (e) { setError(errorMessage(e)); return false; }
    finally { setBusy(false); }
  };
  useEffect(() => {
    if (firstGeneration.current) return;
    firstGeneration.current = true;
    const stale = prefetchIsStale(session.id, currentProfileKey);
    markStudioOpened(session.id, currentProfileKey);
    if (stale) { setDesigns([]); setChosen(null); updateSession((s) => ({ ...s, designs: [] })); void gen(); }
    else if (!session.designs.length || pendingDesignJob(session.id)) void gen();
  }, []);
  useEffect(() => { let alive = true; void quotePrice(spec).then((p) => { if (alive) setPrice({ unit: p.unit, total: p.total }); }).catch(() => undefined); return () => { alive = false; }; }, [spec]);
  const set = <K extends keyof ProductSpec>(key: K, value: ProductSpec[K]) => setSpec((old) => ({ ...old, [key]: value }));
  const order = async () => { if (!chosen) return; setPlacing(true); setError(""); try { const out = await createOrder({ session_id: session.id, design_id: chosen.id, spec }); navigate(`/order/${out.id}`); } catch (e) { setError(errorMessage(e)); setPlacing(false); } };
  return <Page wide><div className="shop-head"><div><Eyebrow>PERSONAL T-SHIRT DESIGNER</Eyebrow><h1>Design your T-shirt.</h1><p>Choose a graphic based on your profile, adjust the shirt, and make a link you can share. This is a quote only; nothing is charged.</p></div><span className="studio-seal">YOUR DESIGN</span></div>
    <Alert>{error}</Alert>{busy && !designs.length && <div className="loading-panel"><span className="loader-dot" />Sketching a few directions…</div>}
    <div className="studio-layout"><div className="studio-main"><p className="studio-step-hint">1. Pick one of the generated graphics. 2. Set the shirt details. 3. Preview and make your link.</p><div className="studio-designs">{designs.map((d, i) => <button aria-pressed={chosen?.id === d.id} className={cx("design-card", chosen?.id === d.id && "design-selected")} onClick={() => { setChosen(d); setSpec(d.recommended_spec); }} key={d.id}><div className="design-preview"><img src={svgDataUrl(d.svg)} alt="" /></div><div><span>DIRECTION 0{i + 1}</span><strong>{d.title}</strong><p>{d.concept}</p></div></button>)}</div>
      {chosen && <div className="selected-design"><div className="selected-preview"><TeeMockup spec={spec} design={chosen} className="studio-shirt" /></div><div><Eyebrow>LIVE SHIRT PREVIEW</Eyebrow><h2>{chosen.title}</h2><p>{chosen.concept}</p><form className="feedback-form" onSubmit={(e) => { e.preventDefault(); const note = feedback.trim(); if (note) void gen(note, chosen).then((ok) => { if (ok) setFeedback(""); }); }}><input aria-label="Design feedback" placeholder="Change the art: simpler, bolder, add a small sun…" value={feedback} onChange={(e) => setFeedback(e.target.value)} /><Button className="button-outline" disabled={busy}>{busy ? "Refining…" : "Refine art ↗"}</Button></form></div></div>}
      <Button className="button-outline" onClick={() => void gen()} disabled={busy}>↻ New directions</Button></div>
      <aside className="spec-panel"><Eyebrow>GARMENT SPEC</Eyebrow><div className="spec-grid">
        <label>Color <input type="color" value={spec.shirt_color} onChange={(e) => set("shirt_color", e.target.value)} /></label>
        <label>Fit <select value={spec.fit} onChange={(e) => set("fit", e.target.value as ProductSpec["fit"])}>{["regular","oversized","slim","boxy","cropped"].map((v) => <option key={v}>{v}</option>)}</select></label>
        <label>Fabric <select value={spec.material} onChange={(e) => set("material", e.target.value as ProductSpec["material"])}>{[["cotton","Cotton"],["organic_cotton","Organic cotton"],["heavyweight_cotton","Heavyweight cotton"],["tri_blend","Tri-blend"],["linen_blend","Linen blend"],["recycled_poly","Recycled poly"]].map(([v,l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label>Neckline <select value={spec.neckline} onChange={(e) => set("neckline", e.target.value as ProductSpec["neckline"])}>{[["crew","Crew neck"],["v_neck","V-neck"],["mock_neck","Mock neck"]].map(([v,l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label>Sleeves <select value={spec.sleeve} onChange={(e) => set("sleeve", e.target.value as ProductSpec["sleeve"])}>{[["short","Short sleeve"],["long","Long sleeve"],["sleeveless","Sleeveless"]].map(([v,l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label>Size <select value={spec.size} onChange={(e) => set("size", e.target.value as ProductSpec["size"])}>{["XS","S","M","L","XL","XXL"].map((v) => <option key={v}>{v}</option>)}</select></label>
        <label>Print <select value={spec.print_method} onChange={(e) => set("print_method", e.target.value as ProductSpec["print_method"])}>{[["dtg","Direct to garment"],["screen","Screen print"],["embroidery","Embroidery"],["puff","Puff ink"]].map(([v,l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label>Placement <select value={spec.placement} onChange={(e) => set("placement", e.target.value as ProductSpec["placement"])}>{[["front_center","Front center"],["left_chest","Left chest"],["back","Back"],["front_and_back","Front + back"]].map(([v,l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label>Quantity <input type="number" min="1" max="50" value={spec.quantity} onChange={(e) => set("quantity", Math.max(1, Math.min(50, Number(e.target.value) || 1)))} /></label>
      </div><label className="spec-notes">Notes <textarea maxLength={500} rows={2} value={spec.notes} onChange={(e) => set("notes", e.target.value)} placeholder="Anything for the print shop?" /></label>
      <div className="estimate"><span>ESTIMATED TOTAL <small>· demo only</small></span><strong>{price ? `$${price.total.toFixed(2)}` : "—"}</strong></div><Button className="button-lime button-full" onClick={() => void order()} disabled={!chosen || !price || placing}>{placing ? "Building your order link…" : "Create shareable order link ↗"}</Button><p className="no-charge">No payment is collected. The link saves your design and shirt specifications.</p></aside>
    </div>
  </Page>;
}

function OrderPage() {
  const { id = "" } = useParams(); const [order, setOrder] = useState<Awaited<ReturnType<typeof getOrder>> | null>(null); const [error, setError] = useState(""); const [copied, setCopied] = useState(false); const [busy, setBusy] = useState(false); const [sent, setSent] = useState(false);
  const [customer, setCustomer] = useState<Customer>({ name: "", email: "", address: "" });
  useEffect(() => { void getOrder(id).then(setOrder).catch((e) => setError(errorMessage(e))); }, [id]);
  const copy = async () => { try { await navigator.clipboard.writeText(window.location.href); setCopied(true); window.setTimeout(() => setCopied(false), 1800); } catch { setError("Your browser blocked copying. Copy the address from the bar at the top of the page."); } };
  const submit = async (e: FormEvent) => { e.preventDefault(); setBusy(true); setError(""); try { setOrder(await requestOrder(id, customer)); setSent(true); } catch (err) { setError(errorMessage(err)); } finally { setBusy(false); } };
  if (!order) return <Page><div className="loading-panel">{error || "Loading order brief…"}</div></Page>;
  return <Page><div className="order-head"><Eyebrow>YOUR SHAREABLE T-SHIRT BRIEF · T:{order.id.slice(0, 6).toUpperCase()}</Eyebrow><h1>Your design,<br />ready to share.</h1><p>Open or send this link to show the design and specifications. This is a class demo: no shirt is produced and no payment is taken.</p>{order.profile_archetype && <div className="order-origin">Designed for <strong>{order.profile_archetype}</strong>{order.country && <> · {order.country.name}</>}</div>}</div><Alert>{error}</Alert>
    <div className="order-layout"><div className="order-art"><TeeMockup spec={order.spec} design={order.design} className="order-shirt" /></div><div className="order-details"><span>THE DESIGN</span><h2>{order.design.title}</h2><p>{order.design.concept}</p><hr /><span>GARMENT DETAILS</span><dl><dt>Fit / size</dt><dd>{order.spec.fit} · {order.spec.size}</dd><dt>Fabric</dt><dd>{order.spec.material.replaceAll("_", " ")}</dd><dt>Neckline / sleeve</dt><dd>{order.spec.neckline.replaceAll("_", " ")} · {order.spec.sleeve}</dd><dt>Print</dt><dd>{order.spec.print_method} · {order.spec.placement.replaceAll("_", " ")}</dd><dt>Quantity</dt><dd>{order.spec.quantity}</dd>{order.price.breakdown.map((line) => <Fragment key={line.label}><dt>{line.label}</dt><dd>${line.amount.toFixed(2)}</dd></Fragment>)}<dt><strong>Total estimate</strong></dt><dd><strong>${order.price.total.toFixed(2)} USD</strong></dd></dl>{order.spec.notes && <p className="order-notes"><b>Print notes</b><br />{order.spec.notes}</p>}<a className="product-link" href={printSvgUrl(id)} download>Download print-ready SVG ↗</a><Button className="button-outline button-full" onClick={() => void copy()}>{copied ? "Link copied ✓" : "Copy share link ↗"}</Button></div></div>
    <div className="checkout-note"><span>JUST A CLASS PROJECT</span><p>No money changes hands here. You can leave a demo request to see the flow end to end.</p></div>
    {order.status !== "requested" || isOrderRequester(id) ? <form className="request-form" onSubmit={(e) => void submit(e)}><Eyebrow>{sent ? "REQUEST RECORDED" : "DEMO CHECKOUT · NO CHARGE"}</Eyebrow><h2>{sent ? "Your demo request is in." : "Try a sample order request"}</h2><p>This records a request in the project database. It does not send a payment or shipping request.</p>{!sent && <><div className="request-fields"><input aria-label="Name" required placeholder="Name" value={customer.name} onChange={(e) => setCustomer({ ...customer, name: e.target.value })} /><input aria-label="Email" type="email" required placeholder="Email" value={customer.email} onChange={(e) => setCustomer({ ...customer, email: e.target.value })} /><input aria-label="Address" required placeholder="Shipping address" value={customer.address} onChange={(e) => setCustomer({ ...customer, address: e.target.value })} /></div><Button className="button-dark" disabled={busy}>{busy ? "Sending…" : "Record demo request ↗"}</Button></>}</form> : <p className="already-requested">A demo request has already been recorded for this brief.</p>}
  </Page>;
}

function Guard({ children, needs = "session" }: { children: ReactNode; needs?: "session" | "activeQuiz" | "quiz" | "profile" }) {
  const { session, status } = useSession();
  if (status === "loading") return <Page><div className="loading-panel">Restoring your saved session…</div></Page>;
  if (!session) return <Navigate to="/" replace />;
  if (needs === "activeQuiz" && session.quiz_done) return <Navigate to="/profile" replace />;
  if (needs === "quiz" && !session.quiz_done) return <Navigate to="/quiz" replace />;
  if (needs === "profile" && !session.profile) return <Navigate to={session.quiz_done ? "/profile" : "/quiz"} replace />;
  return <>{children}</>;
}
function Missing() { return <Page><Eyebrow>THAT PAGE GOT LOST</Eyebrow><h1>Let’s start again.</h1><Link className="button button-dark" to="/">Back to the beginning ↗</Link></Page>; }

export default function App() {
  return <Routes><Route element={<Shell />}><Route index element={<Landing />} />
    <Route path="quiz" element={<Guard needs="activeQuiz"><Quiz /></Guard>} /><Route path="about-you" element={<Guard needs="quiz"><AboutYou /></Guard>} />
    <Route path="profile" element={<Guard needs="quiz"><Profile /></Guard>} /><Route path="shop" element={<Guard needs="profile"><Shop /></Guard>} />
    <Route path="studio" element={<Guard needs="profile"><Studio /></Guard>} /><Route path="order/:id" element={<OrderPage />} /><Route path="*" element={<Missing />} />
  </Route></Routes>;
}
