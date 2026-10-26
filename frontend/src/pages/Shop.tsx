// Shop (SPEC section 2, step 5): 8-12 real, purchasable tees from local, global and indie makers.
// The search takes 30-75 s, so it runs as a shared job (see shopSearch.ts) with a progress panel,
// and results are cached per session + profile. Shortlist lives in localStorage (session.ts).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, errorMessage } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { LoadingProgress } from "../components/commerce/LoadingProgress";
import { NeedProfile } from "../components/commerce/NeedProfile";
import { HeartIcon, ORIGIN_LABEL, ProductCard, ProductSkeleton } from "../components/commerce/ProductCard";
import { ShortlistDrawer } from "../components/commerce/ShortlistDrawer";
import { timeAgo } from "../components/commerce/format";
import { pendingSearch, profileKey, readShopCache, startSearch, type SearchJob } from "../components/commerce/shopSearch";
import { useRequiredSession } from "../SessionContext";
import { toggleShortlist, useShortlist } from "../session";
import type { Origin, Profile, SessionState } from "../types";
import "../styles/commerce.css";

export default function ShopPage() {
  const { session } = useRequiredSession();
  if (!session.profile) return <NeedProfile quizDone={session.quiz_done} purpose="The shop" />;
  return <Shop session={session} profile={session.profile} />;
}

type Filter = Origin | "all";
const FILTERS: Filter[] = ["all", "local", "global", "indie"];

const searchMessages = (country: string) => [
  "Reading your profile and taste palette…",
  `Searching brands from ${country}…`,
  "Scouting global labels…",
  "Digging through indie makers and small studios…",
  "Opening product pages to check they’re real…",
  "Making sure they’re still for sale…",
  "Grabbing product photos…",
  "Balancing local, global and indie picks…",
];

function Shop({ session, profile }: { session: SessionState; profile: Profile }) {
  const key = profileKey(profile);
  const [result, setResult] = useState(() => readShopCache(session.id, key));
  const [job, setJob] = useState<SearchJob | null>(() => pendingSearch(session.id, key));
  const [error, setError] = useState<unknown>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawerButton = useRef<HTMLButtonElement>(null);
  const shortlist = useShortlist();
  const saved = useMemo(() => new Set(shortlist.map((p) => p.url)), [shortlist]);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Listen to a (possibly shared) search job; it keeps running if we unmount.
  const attach = useCallback((j: SearchJob) => {
    setJob(j);
    setError(null);
    j.promise.then(
      (entry) => {
        if (!mounted.current) return;
        setResult(entry);
        setFilter("all");
        setJob((cur) => (cur === j ? null : cur));
      },
      (err: unknown) => {
        if (!mounted.current) return;
        setError(err);
        setJob((cur) => (cur === j ? null : cur));
      },
    );
  }, []);

  const search = useCallback(() => attach(startSearch(session.id, key)), [attach, session.id, key]);

  useEffect(() => {
    const running = pendingSearch(session.id, key); // never a search for an older profile
    if (running) attach(running);
    else if (!readShopCache(session.id, key)) search();
  }, [session.id, key, attach, search]);

  const closeDrawer = useCallback(() => setDrawerOpen(false), []);

  const products = result?.products ?? [];
  const counts = useMemo(() => {
    const c: Record<Filter, number> = { all: products.length, local: 0, global: 0, indie: 0 };
    for (const p of products) c[p.origin] += 1;
    return c;
  }, [products]);
  const visible = filter === "all" ? products : products.filter((p) => p.origin === filter);
  const needsProfile = error instanceof ApiError && error.isConflict;

  return (
    <section className="container section cm-shop">
      <header className="cm-page-head">
        <div className="stack-sm cm-page-head__text">
          <p className="eyebrow">Shop · for {profile.archetype}</p>
          <h1>Shirts that already get you</h1>
          <p className="lead">
            Real tees you can buy now. Cards distinguish a pick mentioned on the retailer page from one found only in the search title.
          </p>
          {session.answers.length > 0 && (
            <p className="cm-picks">
              <span className="cm-picks__label">Built from your picks:</span>
              {session.answers.map((a) => {
                const o = a.choice === "A" ? a.question.option_a : a.question.option_b;
                return (
                  <span key={a.question.id} className="chip">
                    {o.emoji} {o.label}
                  </span>
                );
              })}
            </p>
          )}
        </div>
        <div className="cm-page-head__actions">
          <button
            ref={drawerButton}
            type="button"
            className="btn"
            aria-haspopup="dialog"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen(true)}
          >
            <HeartIcon filled={shortlist.length > 0} />
            Shortlist
            <span className="cm-count" aria-label={`${shortlist.length} saved`}>
              {shortlist.length}
            </span>
          </button>
          <button type="button" className="btn btn-ghost" onClick={search} disabled={Boolean(job)}>
            <span aria-hidden="true">↻</span> Refresh suggestions
          </button>
        </div>
      </header>

      {job && (
        <LoadingProgress
          className="cm-shop__progress"
          title={products.length ? "Finding fresh picks" : "Hunting for real shirts"}
          messages={searchMessages(session.country.name)}
          startedAt={job.startedAt}
          expectedSeconds={50}
          messageEvery={8}
        />
      )}

      {error !== null && !job && (
        needsProfile ? (
          <NeedProfile bare quizDone={session.quiz_done} purpose="The shop" />
        ) : (
          <ErrorBanner
            message={`${errorMessage(error)}${products.length ? " Showing your previous picks." : ""}`}
            onRetry={search}
            onDismiss={() => setError(null)}
          />
        )
      )}

      {products.length > 0 ? (
        <>
          <div className="cm-toolbar">
            <div className="cm-filters" role="group" aria-label="Filter by origin">
              {FILTERS.map((f) => (
                <button
                  key={f}
                  type="button"
                  className="chip"
                  aria-pressed={filter === f}
                  disabled={f !== "all" && counts[f] === 0}
                  onClick={() => setFilter(f)}
                >
                  {f === "all" ? "All" : ORIGIN_LABEL[f]} <span className="cm-filters__n">{counts[f]}</span>
                </button>
              ))}
            </div>
            {result && (
              <p className="tiny muted cm-toolbar__meta">
                {products.length} shirts · found {timeAgo(result.fetchedAt)}
              </p>
            )}
          </div>
          <div className={`cm-product-grid${job ? " is-stale" : ""}`} aria-busy={Boolean(job)}>
            {visible.map((p, i) => (
              <ProductCard
                key={`${p.id}|${p.url}`}
                product={p}
                countryCode={session.country.code}
                countryName={session.country.name}
                shortlisted={saved.has(p.url)}
                onToggleShortlist={toggleShortlist}
                style={{ animationDelay: `${Math.min(i, 11) * 50}ms` }}
              />
            ))}
          </div>
        </>
      ) : job ? (
        <div className="cm-product-grid" aria-hidden="true">
          {Array.from({ length: 8 }, (_, i) => (
            <ProductSkeleton key={i} />
          ))}
        </div>
      ) : result && !error ? (
        <div className="card cm-empty stack-sm">
          <h2>No shirts made the cut this time</h2>
          <p className="muted">The web search came back empty or every link failed our checks. Searching again usually turns something up.</p>
          <div>
            <button type="button" className="btn btn-primary" onClick={search}>
              Search again
            </button>
          </div>
        </div>
      ) : null}

      <aside className="cm-cta-band">
        <div>
          <p className="eyebrow">Nothing quite right?</p>
          <h2 className="cm-cta-band__title">Design one that’s 100% you.</h2>
        </div>
        <Link to="/studio" className="btn btn-primary btn-lg">
          Open the Studio <span aria-hidden="true">→</span>
        </Link>
      </aside>
      <p className="tiny muted cm-fineprint">
        Prices and stock come from each retailer and can change. Buy links open the retailer’s own site in a new tab; Tee:dentity
        doesn’t sell these shirts or earn commission.
      </p>

      <ShortlistDrawer open={drawerOpen} items={shortlist} onClose={closeDrawer} returnFocusRef={drawerButton} />
    </section>
  );
}
