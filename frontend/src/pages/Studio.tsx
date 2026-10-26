// Studio (SPEC section 2, step 6): generate 3 concepts from the profile, pick one, revise it with
// feedback, configure every ProductSpec field against a live mockup + price estimate, then create a
// shareable order link. Selection and spec persist per session in localStorage ("tee.studio").
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, createOrder, errorMessage } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { ConceptPicker } from "../components/commerce/ConceptPicker";
import { ColorPicker, QuantityStepper, Segmented } from "../components/commerce/Controls";
import { LoadingProgress } from "../components/commerce/LoadingProgress";
import { NeedProfile } from "../components/commerce/NeedProfile";
import { PriceTable } from "../components/commerce/OrderDetails";
import { TeeMockup } from "../components/commerce/TeeMockup";
import { SHIRT_COLORS, colorName, contrastRatio, normalizeHex, type NamedColor } from "../components/commerce/color";
import {
  markStudioOpened,
  mergeDesigns,
  pendingDesignJob,
  prefetchIsStale,
  startDesignJob,
  type DesignJob,
  type DesignJobKind,
} from "../components/commerce/designJobs";
import { profileKey } from "../components/commerce/shopSearch";
import { MATERIAL_BASE, PRINT_COST, estimatePrice, formatUSD, nextDiscountTier, discountRate } from "../components/commerce/pricing";
import {
  FITS,
  MATERIALS,
  NECKLINES,
  PLACEMENTS,
  PRINT_METHODS,
  SIZES,
  SLEEVES,
  STYLE_KEYS,
  hintOf,
  labelOf,
  matchesStyle,
  normalizeSpec,
} from "../components/commerce/specOptions";
import { useRequiredSession, useSession } from "../SessionContext";
import { DEFAULT_SPEC, type Design, type DesignGenerateRequest, type Price, type ProductSpec, type Profile, type SessionState } from "../types";
import "../styles/commerce.css";

export default function StudioPage() {
  const { session } = useRequiredSession();
  if (!session.profile) return <NeedProfile quizDone={session.quiz_done} purpose="The Studio" />;
  return <Studio session={session} profile={session.profile} />;
}

// ------------------------------------------------------------------ persistence
const STUDIO_KEY = "tee.studio";

interface SavedStudio {
  sessionId: string;
  selectedId: string | null;
  spec: ProductSpec;
  customized: boolean;
}

function readSaved(sessionId: string): SavedStudio | null {
  try {
    const data = JSON.parse(window.localStorage.getItem(STUDIO_KEY) ?? "null") as Partial<SavedStudio> | null;
    if (!data || data.sessionId !== sessionId) return null;
    return {
      sessionId,
      selectedId: typeof data.selectedId === "string" ? data.selectedId : null,
      spec: normalizeSpec(data.spec),
      customized: Boolean(data.customized),
    };
  } catch {
    return null;
  }
}

function writeSaved(state: SavedStudio): void {
  try {
    window.localStorage.setItem(STUDIO_KEY, JSON.stringify(state));
  } catch {
    /* storage unavailable: state just won't survive a reload */
  }
}

// ------------------------------------------------------------------ copy
const SCRIPT_BY_COUNTRY: Record<string, string> = {
  KR: "Hangul", KP: "Hangul", JP: "Japanese", CN: "Hanzi", TW: "Hanzi", HK: "Hanzi", MO: "Hanzi",
  TH: "Thai script", IN: "Devanagari", NP: "Devanagari", RU: "Cyrillic", UA: "Cyrillic", BG: "Cyrillic",
  RS: "Cyrillic", BY: "Cyrillic", KZ: "Cyrillic", MN: "Mongolian", GR: "Greek", CY: "Greek", IL: "Hebrew",
  SA: "Arabic", AE: "Arabic", EG: "Arabic", MA: "Arabic", DZ: "Arabic", TN: "Arabic", JO: "Arabic",
  LB: "Arabic", QA: "Arabic", KW: "Arabic", OM: "Arabic", BH: "Arabic", IQ: "Arabic", IR: "Persian",
  AM: "Armenian", GE: "Georgian", ET: "Ge’ez", KH: "Khmer", LA: "Lao", MM: "Burmese", LK: "Sinhala",
  BD: "Bengali", PK: "Urdu",
};

/** "Add Hangul" -> "add Hangul" (keeps proper nouns intact when appending). */
const lowerFirst = (text: string) => text.charAt(0).toLowerCase() + text.slice(1);

function feedbackIdeas(countryCode: string): string[] {
  const script = SCRIPT_BY_COUNTRY[countryCode];
  return ["Bolder", "More minimal", script ? `Add ${script}` : "Add a local word", "Retro vibe", "Fewer colors", "Bigger type", "Make it funnier"];
}

const JOB_COPY: Record<DesignJobKind, { title: string; seconds: number; messages: string[] }> = {
  initial: {
    title: "Sketching your first 3 concepts",
    seconds: 40,
    messages: [
      "Reading your taste palette…",
      "Pulling motifs from your cultural roots…",
      "Sketching three different directions…",
      "Choosing a tight color palette…",
      "Setting the type…",
      "Cleaning up the vectors…",
      "Checking the files are print-safe…",
    ],
  },
  more: {
    title: "Sketching 3 more concepts",
    seconds: 40,
    messages: ["Looking for new directions…", "Sketching three fresh ideas…", "Choosing palettes…", "Cleaning up the vectors…", "Checking the files are print-safe…"],
  },
  revise: {
    title: "Revising your concept",
    seconds: 30,
    messages: ["Reading your feedback…", "Redrawing the concept…", "Rebalancing color and type…", "Cleaning up the vectors…"],
  },
};

/** Copy a design's recommended look (not size / quantity / notes) onto the current spec. */
function withStyle(spec: ProductSpec, recommended: ProductSpec): ProductSpec {
  const rec = normalizeSpec(recommended);
  const next = { ...spec };
  for (const k of STYLE_KEYS) (next as Record<string, unknown>)[k] = rec[k];
  return next;
}

// ------------------------------------------------------------------ page
function Studio({ session, profile }: { session: SessionState; profile: Profile }) {
  const { updateSession } = useSession();
  const navigate = useNavigate();
  const ids = { material: useId(), notes: useId(), feedback: useId() };
  const designs = session.designs;

  const palette: NamedColor[] = useMemo(
    () => profile.taste.colors.flatMap((c) => (normalizeHex(c.hex) ? [{ hex: normalizeHex(c.hex)!, name: c.name }] : [])),
    [profile],
  );

  const [saved] = useState(() => readSaved(session.id));
  const [selectedId, setSelectedId] = useState<string | null>(() =>
    saved?.selectedId && designs.some((d) => d.id === saved.selectedId) ? saved.selectedId : designs[0]?.id ?? null,
  );
  const [spec, setSpec] = useState<ProductSpec>(() => {
    if (saved && (saved.customized || saved.selectedId)) return saved.spec;
    // Never styled yet (e.g. the first concepts landed while the user was on another page, so the
    // saved spec is the default written while there were no designs): apply the recommendation.
    const first = designs[0];
    return first ? withStyle(saved?.spec ?? DEFAULT_SPEC, first.recommended_spec) : saved?.spec ?? DEFAULT_SPEC;
  });
  const [customized, setCustomized] = useState(saved?.customized ?? false);
  const customizedRef = useRef(customized);
  customizedRef.current = customized;

  const [job, setJob] = useState<DesignJob | null>(() => pendingDesignJob(session.id));
  const [jobError, setJobError] = useState<{ err: unknown; job: DesignJob } | null>(null);
  const [feedback, setFeedback] = useState("");
  const [orderError, setOrderError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const selected: Design | null = designs.find((d) => d.id === selectedId) ?? designs[0] ?? null;

  useEffect(() => {
    writeSaved({ sessionId: session.id, selectedId: selected?.id ?? null, spec, customized });
  }, [session.id, selected?.id, spec, customized]);

  // ---------------------------------------------------------------- design jobs
  const attach = useCallback((j: DesignJob) => {
    setJob(j);
    setJobError(null);
    j.promise.then(
      (fresh) => {
        if (!mounted.current) return;
        setJob((cur) => (cur === j ? null : cur));
        if (!fresh.length) return;
        setSelectedId(fresh[0].id);
        if (j.kind === "revise") setFeedback("");
        else if (!customizedRef.current) setSpec((s) => withStyle(s, fresh[0].recommended_spec));
      },
      (err: unknown) => {
        if (!mounted.current) return;
        setJob((cur) => (cur === j ? null : cur));
        setJobError({ err, job: j });
      },
    );
  }, []);

  const run = useCallback(
    (kind: DesignJobKind, req: DesignGenerateRequest) =>
      attach(
        startDesignJob(session.id, kind, req, (fresh) =>
          updateSession((s) => (s.id === session.id ? { ...s, designs: mergeDesigns(s.designs, fresh) } : s)),
        ),
      ),
    [attach, session.id, updateSession],
  );

  // On entry: re-attach to a running job (usually the Profile page's prefetch), or generate the
  // first 3 concepts if there are none. If the prefetched concepts were drawn for a profile that was
  // regenerated before Studio was ever opened, draw fresh ones for the current profile.
  useEffect(() => {
    const key = profileKey(profile);
    const stale = prefetchIsStale(session.id, key);
    const initial = () => {
      markStudioOpened(session.id, key);
      run("initial", { feedback: null, base_design_id: null, count: 3 });
    };
    const running = pendingDesignJob(session.id);
    if (running) {
      attach(running);
      if (stale) void running.promise.catch(() => undefined).then(() => mounted.current && initial());
      else markStudioOpened(session.id, key);
    } else if (session.designs.length === 0 || stale) initial();
    else markStudioOpened(session.id, key);
    // Deliberately keyed on the session only: later generations are user-triggered.
  }, [session.id]);

  const revise = () => {
    const text = feedback.trim();
    if (!selected || !text || job) return;
    run("revise", { feedback: text, base_design_id: selected.id, count: 1 });
  };
  const moreConcepts = () => {
    if (job) return;
    run("more", { feedback: feedback.trim() || null, base_design_id: null, count: 3 });
  };

  // ---------------------------------------------------------------- spec
  const patch = (changes: Partial<ProductSpec>) => {
    setSpec((s) => ({ ...s, ...changes }));
    setCustomized(true);
  };
  const select = (id: string) => {
    setSelectedId(id);
    const d = designs.find((x) => x.id === id);
    if (d && !customizedRef.current) setSpec((s) => withStyle(s, d.recommended_spec));
  };
  const applySuggested = () => {
    if (!selected) return;
    setSpec((s) => withStyle(s, selected.recommended_spec));
    setCustomized(false);
  };

  const price = useMemo(() => estimatePrice(spec), [spec]);
  const shirtName = colorName(spec.shirt_color, palette);
  const recommended = selected ? normalizeSpec(selected.recommended_spec) : null;
  const lowContrast = useMemo(
    () =>
      (selected?.palette ?? [])
        .map((c) => normalizeHex(c))
        .filter((c): c is string => c !== null && contrastRatio(c, spec.shirt_color) < 1.3),
    [selected, spec.shirt_color],
  );

  // ---------------------------------------------------------------- order
  const createLink = async () => {
    if (!selected || creating) return;
    setCreating(true);
    setOrderError(null);
    try {
      const order = await createOrder({
        session_id: session.id,
        design_id: selected.id,
        spec: normalizeSpec({ ...spec, notes: spec.notes.trim() }),
      });
      navigate(`/order/${encodeURIComponent(order.id)}`);
    } catch (err) {
      if (!mounted.current) return;
      setOrderError(errorMessage(err));
      setCreating(false);
    }
  };

  const qty = spec.quantity;
  const tier = nextDiscountTier(qty);
  const rate = discountRate(qty);
  const qtyHint =
    (rate ? `${Math.round(rate * 100)}% bulk discount applied. ` : "") +
    (tier ? `Add ${tier.min - qty} more for ${Math.round(tier.rate * 100)}% off.` : "Max bulk discount reached.");

  const pending = job ? (job.kind === "revise" ? 1 : 3) : 0;
  const jobCopy = job ? JOB_COPY[job.kind] : null;
  const jobErr = jobError ? describeJobError(jobError.err) : null;

  const checkout = (variant: "desk" | "mobile") => (
    <CheckoutPanel
      className={`cm-studio__checkout cm-studio__checkout--${variant}`}
      price={price}
      quantity={qty}
      canOrder={Boolean(selected) && !creating}
      creating={creating}
      error={orderError}
      onDismissError={() => setOrderError(null)}
      onCreate={createLink}
      waitingForDesign={!selected}
    />
  );

  return (
    <section className="container section cm-studio">
      <header className="cm-page-head">
        <div className="stack-sm cm-page-head__text">
          <p className="eyebrow">Studio · for {profile.archetype}</p>
          <h1>Design your tee</h1>
          {profile.tshirt_brief && <p className="lead cm-studio__brief">{profile.tshirt_brief}</p>}
        </div>
        <div className="cm-page-head__actions">
          <Link to="/shop" className="btn btn-ghost">
            Browse real shirts instead
          </Link>
        </div>
      </header>

      <div className="cm-studio__grid">
        {/* ---------------------------------------------------- left: mockup + checkout */}
        <aside className="cm-studio__aside" aria-label="Preview and price">
          <div className="card cm-mockup-card">
            <TeeMockup spec={spec} design={selected} loading={Boolean(job) && !selected} colorNames={palette} />
            <p className="cm-mockup-card__summary">
              {[shirtName, labelOf(FITS, spec.fit), labelOf(MATERIALS, spec.material), labelOf(PLACEMENTS, spec.placement), `Size ${spec.size}`].join(" · ")}
            </p>
          </div>
          {checkout("desk")}
        </aside>

        {/* ---------------------------------------------------- right: concepts + controls */}
        <div className="cm-studio__main">
          <div className="card cm-concept-card">
            <div className="row-between">
              <h2 className="cm-card-title">Concepts</h2>
              <button type="button" className="btn btn-sm" onClick={moreConcepts} disabled={Boolean(job)}>
                + 3 new concepts
              </button>
            </div>

            {job && jobCopy && (
              <LoadingProgress title={jobCopy.title} messages={jobCopy.messages} startedAt={job.startedAt} expectedSeconds={jobCopy.seconds} messageEvery={6} />
            )}
            {jobError && jobErr && !job && (
              jobErr.needsProfile ? (
                <NeedProfile bare quizDone={session.quiz_done} purpose="The Studio" />
              ) : (
                <ErrorBanner
                  message={jobErr.message}
                  onRetry={() => run(jobError.job.kind, jobError.job.req)}
                  onDismiss={() => setJobError(null)}
                />
              )
            )}

            {(designs.length > 0 || pending > 0) && (
              <ConceptPicker designs={designs} selectedId={selected?.id ?? null} onSelect={select} background={spec.shirt_color} pending={pending} />
            )}

            {selected && (
              <div className="cm-concept-detail fade-in" key={selected.id}>
                <h3 className="cm-concept-detail__title">{selected.title}</h3>
                <p className="cm-concept-detail__text">{selected.concept}</p>
                {selected.palette.length > 0 && (
                  <div className="cm-palette" aria-label="Design colors">
                    {selected.palette.map((hex, i) => (
                      <span key={`${hex}-${i}`} className="cm-palette__dot" style={{ background: hex }} title={hex.toUpperCase()} />
                    ))}
                  </div>
                )}
                {lowContrast.length > 0 && (
                  <p className="cm-warn" role="status">
                    Heads up: {lowContrast.length === 1 ? "one of this design’s colors" : `${lowContrast.length} of this design’s colors`} nearly
                    matches the {shirtName.toLowerCase()} shirt and may disappear.
                  </p>
                )}
                {recommended && !matchesStyle(spec, recommended) && (
                  <button type="button" className="btn btn-ghost btn-sm cm-btn-quiet cm-concept-detail__suggest" onClick={applySuggested}>
                    Use this concept’s suggested setup
                  </button>
                )}
              </div>
            )}

            {designs.length > 0 && (
              <form
                className="cm-revise"
                onSubmit={(e) => {
                  e.preventDefault();
                  revise();
                }}
              >
                <label className="label" htmlFor={ids.feedback}>
                  Tweak this concept
                </label>
                <textarea
                  id={ids.feedback}
                  className="textarea cm-revise__input"
                  rows={2}
                  maxLength={500}
                  value={feedback}
                  onChange={(e) => setFeedback(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) revise();
                  }}
                  placeholder="e.g. “bolder”, “add a local word”, “more minimal”"
                />
                <div className="cm-revise__ideas" role="group" aria-label="Feedback ideas">
                  {feedbackIdeas(session.country.code).map((idea) => (
                    <button
                      key={idea}
                      type="button"
                      className="chip chip-soft"
                      onClick={() => setFeedback((f) => (f.trim() ? `${f.trim().replace(/[,.]$/, "")}, ${lowerFirst(idea)}` : idea))}
                    >
                      + {idea}
                    </button>
                  ))}
                </div>
                <div className="row">
                  <button type="submit" className="btn btn-blue" disabled={!selected || !feedback.trim() || Boolean(job)}>
                    Revise this concept
                  </button>
                  <span className="tiny muted">Makes a new version; the original stays in the strip.</span>
                </div>
              </form>
            )}
          </div>

          <div className="card cm-controls">
            <h2 className="cm-card-title">Build the shirt</h2>
            <ColorPicker
              value={spec.shirt_color}
              onChange={(hex) => patch({ shirt_color: hex })}
              palette={palette}
              classics={SHIRT_COLORS}
              valueName={shirtName}
            />
            <Segmented legend="Fit" value={spec.fit} options={FITS} onChange={(fit) => patch({ fit })} hint={hintOf(FITS, spec.fit)} />
            <div className="cm-field">
              <label className="label cm-field__legend" htmlFor={ids.material}>
                Material
              </label>
              <select
                id={ids.material}
                className="select"
                value={spec.material}
                onChange={(e) => patch({ material: e.target.value as ProductSpec["material"] })}
              >
                {MATERIALS.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label} · from {formatUSD(MATERIAL_BASE[m.value])}
                  </option>
                ))}
              </select>
              <p className="hint cm-field__hint">{hintOf(MATERIALS, spec.material)}</p>
            </div>
            <div className="cm-controls__pair">
              <Segmented legend="Neckline" value={spec.neckline} options={NECKLINES} onChange={(neckline) => patch({ neckline })} />
              <Segmented
                legend="Sleeves"
                value={spec.sleeve}
                options={SLEEVES}
                onChange={(sleeve) => patch({ sleeve })}
                extra={(v) => (v === "long" ? "+$5" : undefined)}
              />
            </div>
            <Segmented legend="Size" value={spec.size} options={SIZES} onChange={(size) => patch({ size })} extra={(v) => (v === "XXL" ? "+$3" : undefined)} />
            <Segmented
              legend="Print method"
              value={spec.print_method}
              options={PRINT_METHODS}
              onChange={(print_method) => patch({ print_method })}
              extra={(v) => `$${PRINT_COST[v]}`}
              hint={
                <>
                  {hintOf(PRINT_METHODS, spec.print_method)}
                  {spec.print_method === "embroidery" && spec.placement !== "left_chest" && (
                    <strong className="cm-hint-strong"> Tip: embroidery shines small; try Left chest.</strong>
                  )}
                </>
              }
            />
            <Segmented
              legend="Placement"
              value={spec.placement}
              options={PLACEMENTS}
              onChange={(placement) => patch({ placement })}
              extra={(v) => (v === "front_and_back" ? "+$7" : undefined)}
              hint={hintOf(PLACEMENTS, spec.placement)}
            />
            <QuantityStepper value={qty} onChange={(quantity) => patch({ quantity })} hint={qtyHint} />
            <div className="cm-field">
              <label className="label cm-field__legend" htmlFor={ids.notes}>
                Notes for the printer <span className="muted">(optional)</span>
              </label>
              <textarea
                id={ids.notes}
                className="textarea"
                rows={3}
                maxLength={500}
                value={spec.notes}
                onChange={(e) => patch({ notes: e.target.value.slice(0, 500) })}
                placeholder="e.g. Print a little higher, matte ink, gift wrap please."
              />
              <p className="hint cm-field__hint cm-counter">{spec.notes.length}/500</p>
            </div>
          </div>

          {checkout("mobile")}
        </div>
      </div>
    </section>
  );
}

function describeJobError(err: unknown): { message: string; needsProfile: boolean } {
  if (err instanceof ApiError && err.isConflict) return { message: err.detail, needsProfile: true };
  if (err instanceof ApiError && err.status === 501) {
    return { message: "Design generation isn’t switched on yet on this server.", needsProfile: false };
  }
  return { message: errorMessage(err), needsProfile: false };
}

// ------------------------------------------------------------------ checkout panel
interface CheckoutPanelProps {
  className: string;
  price: Price;
  quantity: number;
  canOrder: boolean;
  creating: boolean;
  waitingForDesign: boolean;
  error: string | null;
  onDismissError: () => void;
  onCreate: () => void;
}

function CheckoutPanel({ className, price, quantity, canOrder, creating, waitingForDesign, error, onDismissError, onCreate }: CheckoutPanelProps) {
  return (
    <div className={`card ${className}`}>
      <div className="row-between cm-checkout__head">
        <h2 className="cm-card-title">Price</h2>
        <span className="chip chip-soft">Estimate</span>
      </div>
      <PriceTable price={price} quantity={quantity} estimate collapsible caption="Price estimate" />
      {error && <ErrorBanner message={error} onDismiss={onDismissError} />}
      <button type="button" className="btn btn-primary btn-lg btn-block" onClick={onCreate} disabled={!canOrder}>
        {creating ? "Creating link…" : "Create order link"}
      </button>
      <p className="tiny muted cm-checkout__note">
        {waitingForDesign ? "Pick a concept first." : "Saves this design + setup to a shareable page. No payment."}
      </p>
    </div>
  );
}
