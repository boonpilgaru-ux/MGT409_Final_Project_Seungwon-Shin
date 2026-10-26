// Shop product card, product image with graceful fallback, origin chip and heart icon.
import { useEffect, useState, type CSSProperties } from "react";
import type { Origin, Product } from "../../types";
import { flagEmoji } from "../../countries";
import { formatProductPrice, hashString, hostOf, safeHttpUrl } from "./format";

const FALLBACK_GRADIENTS = [
  ["#ff3b30", "#ff9f0a"],
  ["#2f5bff", "#7c9cff"],
  ["#141414", "#4a4a48"],
  ["#12a150", "#8bd3a6"],
  ["#b9a6d9", "#f3c4cf"],
  ["#e0a526", "#ffe08a"],
];

/** Product photo; falls back to a branded gradient tile if missing, broken or a tracking pixel. */
export function ProductImage({ src, alt, seed, className = "" }: { src: string | null; alt: string; seed: string; className?: string }) {
  const safe = safeHttpUrl(src);
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [safe]);

  if (!safe || failed) {
    const [a, b] = FALLBACK_GRADIENTS[hashString(seed) % FALLBACK_GRADIENTS.length];
    return (
      <div className={`cm-img cm-img--fallback ${className}`.trim()} style={{ background: `linear-gradient(135deg, ${a}, ${b})` }} role="img" aria-label={alt}>
        <TeeGlyph />
        <span className="cm-img__initial" aria-hidden="true">
          {seed.trim().charAt(0).toUpperCase() || "T"}
        </span>
      </div>
    );
  }
  return (
    <img
      className={`cm-img ${className}`.trim()}
      src={safe}
      alt={alt}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
      onLoad={(e) => {
        if (e.currentTarget.naturalWidth < 40) setFailed(true); // spacer / tracking pixel
      }}
    />
  );
}

function TeeGlyph() {
  return (
    <svg className="cm-img__glyph" viewBox="0 0 64 56" aria-hidden="true">
      <path
        d="M22 4 L10 9 L2 22 L12 27 L15 22 L15 52 L49 52 L49 22 L52 27 L62 22 L54 9 L42 4 Q32 13 22 4 Z"
        fill="rgb(255 255 255 / 0.9)"
        stroke="rgb(20 20 20 / 0.85)"
        strokeWidth="2.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function HeartIcon({ filled }: { filled: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
      <path
        d="M12 20.5s-7.5-4.6-9.3-9.4C1.5 7.8 3.6 4.5 7 4.5c2 0 3.6 1.1 5 3 1.4-1.9 3-3 5-3 3.4 0 5.5 3.3 4.3 6.6-1.8 4.8-9.3 9.4-9.3 9.4z"
        fill={filled ? "currentColor" : "none"}
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export const ORIGIN_LABEL: Record<Origin, string> = { local: "Local brand", global: "Global brand", indie: "Indie maker" };

export function OriginChip({ origin, countryCode, countryName }: { origin: Origin; countryCode: string; countryName: string }) {
  const title = origin === "local" ? `A brand from ${countryName}` : origin === "global" ? "Big international label" : "Independent designer or small maker";
  return (
    <span className={`chip cm-origin cm-origin--${origin}`} title={title}>
      {origin === "local" && <span aria-hidden="true">{flagEmoji(countryCode)}</span>}
      {ORIGIN_LABEL[origin]}
    </span>
  );
}

interface ProductCardProps {
  product: Product;
  countryCode: string;
  countryName: string;
  shortlisted: boolean;
  onToggleShortlist: (product: Product) => void;
  style?: CSSProperties;
}

export function ProductCard({ product, countryCode, countryName, shortlisted, onToggleShortlist, style }: ProductCardProps) {
  const href = safeHttpUrl(product.url);
  const price = formatProductPrice(product.price, product.currency);
  const host = hostOf(product.url);
  const showHost = host && !product.retailer.toLowerCase().replace(/\s+/g, "").includes(host.split(".")[0]);

  return (
    <article className="cm-product card slide-up" style={style}>
      <div className="cm-product__media">
        <ProductImage src={product.image_url} alt={product.title} seed={product.brand || product.retailer} />
        <OriginChip origin={product.origin} countryCode={countryCode} countryName={countryName} />
        <button
          type="button"
          className={`cm-heart${shortlisted ? " is-on" : ""}`}
          aria-pressed={shortlisted}
          aria-label={`Shortlist ${product.title}`}
          title={shortlisted ? "Remove from shortlist" : "Save to shortlist"}
          onClick={() => onToggleShortlist(product)}
        >
          <HeartIcon filled={shortlisted} />
        </button>
      </div>
      <div className="cm-product__body">
        <p className="cm-product__brand">{product.brand}</p>
        <h3 className="cm-product__title">{product.title}</h3>
        <p className="cm-product__meta">
          <span>{product.retailer}</span>
          {showHost && <span className="muted"> · {host}</span>}
        </p>
        <p className={`cm-product__price${price ? "" : " muted"}`}>{price ?? "Price on site"}</p>
        {product.pick_match && (
          <div className={`cm-product__pick cm-product__pick--${product.pick_match_source ?? "search_result"}`}>
            <span className="cm-product__why-label">
              {product.pick_match_source === "product_page" ? "Picked in your game · page match" : "Possible pick · search title"}
            </span>
            <strong>{product.pick_match}</strong>
          </div>
        )}
        {product.why && (
          <div className="cm-product__why">
            <span className="cm-product__why-label">{product.pick_match ? "Style note" : "Profile style note"}</span>
            <p>{product.why}</p>
          </div>
        )}
        <div className="cm-product__actions">
          {href ? (
            <a className="btn btn-dark btn-block" href={href} target="_blank" rel="noopener noreferrer">
              Buy <span aria-hidden="true">↗</span>
              <span className="visually-hidden"> {product.title} at {product.retailer} (opens in a new tab)</span>
            </a>
          ) : (
            <span className="btn btn-block" aria-disabled="true">
              Link unavailable
            </span>
          )}
        </div>
      </div>
    </article>
  );
}

/** Skeleton stand-in for a product card while the search runs. */
export function ProductSkeleton() {
  return (
    <div className="cm-product card cm-product--skeleton" aria-hidden="true">
      <div className="skeleton cm-product__media-skel" />
      <div className="cm-product__body">
        <div className="skeleton skeleton-text" style={{ width: "40%" }} />
        <div className="skeleton skeleton-text" style={{ width: "85%", height: "1.2em" }} />
        <div className="skeleton skeleton-text" style={{ width: "55%" }} />
        <div className="skeleton skeleton-text" style={{ width: "30%" }} />
        <div className="skeleton" style={{ height: 64, marginTop: 8 }} />
        <div className="skeleton" style={{ height: 44, marginTop: 12, borderRadius: 999 }} />
      </div>
    </div>
  );
}
