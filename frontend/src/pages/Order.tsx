// Order (SPEC section 2, step 7): shareable page that works for anyone with the link (no session).
// Mockup, spec sheet, server price, concept + print file download, copy link, demo checkout.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, errorMessage, getOrder, isAbortError, isOrderRequester, printSvgUrl, requestOrder, svgDataUrl } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { PageLoader } from "../components/Spinner";
import { CopyLink } from "../components/commerce/CopyLink";
import { PriceTable, SpecSheet } from "../components/commerce/OrderDetails";
import { TeeMockup } from "../components/commerce/TeeMockup";
import { looksLikeEmail, maskEmail } from "../components/commerce/format";
import { flagEmoji } from "../countries";
import { useSession } from "../SessionContext";
import type { Order } from "../types";
import "../styles/commerce.css";

type LoadState =
  | { status: "loading" }
  | { status: "ready"; order: Order }
  | { status: "notfound" }
  | { status: "error"; message: string };

export default function OrderPage() {
  const { id = "" } = useParams<{ id: string }>();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const ac = new AbortController();
    setState({ status: "loading" });
    getOrder(id, ac.signal).then(
      (order) => setState({ status: "ready", order }),
      (err: unknown) => {
        if (isAbortError(err)) return;
        if (err instanceof ApiError && err.isNotFound) setState({ status: "notfound" });
        else setState({ status: "error", message: errorMessage(err) });
      },
    );
    return () => ac.abort();
  }, [id, attempt]);

  if (state.status === "loading") return <PageLoader label="Loading order…" />;
  if (state.status === "notfound") {
    return (
      <section className="container section center stack">
        <p className="eyebrow">Order not found</p>
        <h1>This order link doesn’t exist.</h1>
        <p className="lead">Double-check the link, or make a shirt of your own.</p>
        <p>
          <Link to="/" className="btn btn-primary">
            Start Tee:dentity
          </Link>
        </p>
      </section>
    );
  }
  if (state.status === "error") {
    return (
      <section className="container section">
        <ErrorBanner message={state.message} onRetry={() => setAttempt((n) => n + 1)} />
      </section>
    );
  }
  return <OrderView order={state.order} onUpdate={(order) => setState({ status: "ready", order })} />;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function OrderView({ order, onUpdate }: { order: Order; onUpdate: (order: Order) => void }) {
  const { session } = useSession();
  const shareUrl = `${window.location.origin}/order/${encodeURIComponent(order.id)}`;
  const printHref = printSvgUrl(order.id);
  const printName = `teedentity-${order.id}.svg`;
  const requested = order.status === "requested";

  useEffect(() => {
    const previous = document.title;
    document.title = `${order.design.title} · Tee:dentity order`;
    return () => {
      document.title = previous;
    };
  }, [order.design.title]);

  return (
    <section className="container section cm-order">
      <header className="cm-page-head">
        <div className="stack-sm cm-page-head__text">
          <p className="eyebrow">
            Order <span className="mono">#{order.id}</span>
          </p>
          <h1>{order.design.title}</h1>
          <div className="row cm-order__chips">
            <span className={`chip ${requested ? "badge-ok" : "chip-soft"}`}>{requested ? "✓ Requested" : "Draft"}</span>
            {order.profile_archetype && <span className="chip chip-soft">For {order.profile_archetype}</span>}
            {order.country && (
              <span className="chip chip-soft">
                <span aria-hidden="true">{flagEmoji(order.country.code)}</span> {order.country.name}
              </span>
            )}
            <span className="tiny muted">
              Created <time dateTime={order.created_at}>{formatDate(order.created_at)}</time>
            </span>
          </div>
        </div>
        <div className="cm-page-head__actions">
          {session ? (
            <Link to="/studio" className="btn btn-ghost">
              <span aria-hidden="true">←</span> Back to Studio
            </Link>
          ) : (
            <Link to="/" className="btn btn-ghost">
              Make your own
            </Link>
          )}
          <a className="btn" href={printHref} download={printName}>
            Download print file
          </a>
        </div>
      </header>

      <div className="cm-order__grid">
        <div className="cm-order__visual">
          <div className="card cm-mockup-card">
            <TeeMockup spec={order.spec} design={order.design} />
          </div>
          <div className="card cm-order__concept">
            <h2 className="cm-card-title">The concept</h2>
            <p className="cm-order__concept-text">{order.design.concept}</p>
            <div className="cm-artwork">
              <div className="cm-artwork__tile">
                <img src={svgDataUrl(order.design.svg)} alt={`Print artwork: ${order.design.title}`} />
              </div>
              <div className="cm-artwork__info">
                {order.design.palette.length > 0 && (
                  <div className="cm-palette" aria-label="Design colors">
                    {order.design.palette.map((hex, i) => (
                      <span key={`${hex}-${i}`} className="cm-palette__dot" style={{ background: hex }} title={hex.toUpperCase()} />
                    ))}
                  </div>
                )}
                <a className="btn btn-dark btn-sm" href={printHref} download={printName}>
                  Download print-ready SVG
                </a>
                <p className="tiny muted">Vector artwork, square 1000 × 1000 canvas, transparent background.</p>
              </div>
            </div>
          </div>
        </div>

        <div className="cm-order__info">
          <div className="card">
            <h2 className="cm-card-title">Spec sheet</h2>
            <SpecSheet spec={order.spec} />
          </div>
          <div className="card">
            <h2 className="cm-card-title">Price</h2>
            <PriceTable price={order.price} quantity={order.spec.quantity} caption="Price breakdown" />
          </div>
          <div className="card">
            <CopyLink url={shareUrl} label="Share this order" />
          </div>
          <div className="card cm-checkout" id="checkout">
            <DemoCheckout order={order} onUpdate={onUpdate} />
          </div>
        </div>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ demo checkout
function DemoCheckout({ order, onUpdate }: { order: Order; onUpdate: (order: Order) => void }) {
  const { session } = useSession();
  const requested = order.status === "requested" && order.customer !== null;
  // Only the browser that placed the request holds its token and may see or change the details.
  const isRequester = isOrderRequester(order.id);
  const [editing, setEditing] = useState(!requested);
  const [justPlaced, setJustPlaced] = useState(false);
  // Only the name is prefilled: the page is shareable, so don't echo someone's email/address.
  const [name, setName] = useState(order.customer?.name ?? "");
  const [email, setEmail] = useState("");
  const [address, setAddress] = useState("");
  const [showErrors, setShowErrors] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const doneHeading = useRef<HTMLHeadingElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const addressRef = useRef<HTMLTextAreaElement>(null);

  const errors = {
    name: name.trim() ? null : "Enter your name.",
    email: looksLikeEmail(email) ? null : "Enter a valid email address.",
    address: address.trim() ? null : "Enter a shipping address.",
  };
  const invalid = Boolean(errors.name || errors.email || errors.address);

  useEffect(() => {
    if (!editing && justPlaced) doneHeading.current?.focus();
  }, [editing, justPlaced]);

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setShowErrors(true);
    if (invalid) {
      (errors.name ? nameRef : errors.email ? emailRef : addressRef).current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const updated = await requestOrder(order.id, { name: name.trim(), email: email.trim(), address: address.trim() });
      onUpdate(updated);
      setEditing(false);
      setJustPlaced(true);
      setShowErrors(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (!editing && order.customer && !isRequester) {
    return (
      <div className="cm-confirm">
        <div className="cm-confirm__badge" aria-hidden="true">
          ✓
        </div>
        <h2 className="cm-card-title">This order has already been requested</h2>
        <p className="small muted">Demo checkout: nothing was charged and nothing will ship.</p>
        <Link to={session ? "/studio" : "/"} className="btn btn-primary btn-sm">
          Make your own
        </Link>
      </div>
    );
  }

  if (!editing && order.customer) {
    return (
      <div className="cm-confirm pop-in">
        <div className="cm-confirm__badge" aria-hidden="true">
          ✓
        </div>
        <h2 className="cm-card-title" ref={doneHeading} tabIndex={-1}>
          {justPlaced ? "Order requested!" : "This order has been requested"}
        </h2>
        <p>
          {order.spec.quantity} × “{order.design.title}” requested by <strong>{order.customer.name}</strong>{" "}
          <span className="muted">({maskEmail(order.customer.email)})</span>.
        </p>
        <p className="small muted">Demo checkout: nothing was charged and nothing will ship.</p>
        <button
          type="button"
          className="btn btn-ghost btn-sm cm-btn-quiet"
          onClick={() => {
            setEditing(true);
            setJustPlaced(false);
            window.setTimeout(() => nameRef.current?.focus(), 0);
          }}
        >
          Update details
        </button>
      </div>
    );
  }

  const fieldError = (key: keyof typeof errors) => (showErrors ? errors[key] : null);

  return (
    <form className="cm-form" noValidate onSubmit={submit}>
      <div className="row-between">
        <h2 className="cm-card-title">Place order</h2>
        <span className="chip badge-limited">Demo · no charge</span>
      </div>
      <p className="small muted">
        This is a demo checkout: we record your request, but no payment is taken and nothing ships.
      </p>

      <label className="field">
        <span className="label">Name</span>
        <input
          ref={nameRef}
          className="input"
          name="name"
          autoComplete="name"
          maxLength={120}
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-invalid={Boolean(fieldError("name"))}
          aria-describedby={fieldError("name") ? "co-name-err" : undefined}
        />
        {fieldError("name") && (
          <span className="cm-form__err" id="co-name-err">
            {fieldError("name")}
          </span>
        )}
      </label>
      <label className="field">
        <span className="label">Email</span>
        <input
          ref={emailRef}
          className="input"
          type="email"
          name="email"
          autoComplete="email"
          inputMode="email"
          maxLength={254}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          aria-invalid={Boolean(fieldError("email"))}
          aria-describedby={fieldError("email") ? "co-email-err" : undefined}
        />
        {fieldError("email") && (
          <span className="cm-form__err" id="co-email-err">
            {fieldError("email")}
          </span>
        )}
      </label>
      <label className="field">
        <span className="label">Shipping address</span>
        <textarea
          ref={addressRef}
          className="textarea"
          name="address"
          autoComplete="street-address"
          rows={3}
          maxLength={1000}
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          aria-invalid={Boolean(fieldError("address"))}
          aria-describedby={fieldError("address") ? "co-address-err" : undefined}
        />
        {fieldError("address") && (
          <span className="cm-form__err" id="co-address-err">
            {fieldError("address")}
          </span>
        )}
      </label>
      {requested && <p className="tiny muted">Re-enter your email and address to update this request.</p>}

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={busy}>
        {busy ? "Placing order…" : "Place order (demo — no charge)"}
      </button>
      {requested && (
        <button type="button" className="btn btn-ghost btn-sm cm-btn-quiet" onClick={() => setEditing(false)}>
          Cancel
        </button>
      )}
    </form>
  );
}
