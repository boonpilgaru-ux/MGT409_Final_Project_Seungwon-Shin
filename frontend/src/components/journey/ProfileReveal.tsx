// The profile "reveal": archetype poster, trait meters, taste palette, cultural roots, values,
// and the t-shirt brief with the Shop / Studio calls to action.
import type { CSSProperties, ReactNode } from "react";
import { Link } from "react-router-dom";
import { flagEmoji } from "../../countries";
import type { Country, Profile, UsageSummary } from "../../types";
import { safeColor } from "./journeyUtils";
import { TraitBar } from "./TraitBar";

interface ProfileRevealProps {
  profile: Profile;
  country: Country;
  answerCount: number;
  contextCount: number;
  usage?: UsageSummary | null;
  /** Extra controls rendered in the footer row (e.g. Regenerate). */
  actions?: ReactNode;
}

const paragraphs = (text: string) =>
  text
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);

function ChipList({ items, variant = "", label }: { items: string[]; variant?: string; label: string }) {
  if (!items.length) return null;
  return (
    <ul className="j-chips" aria-label={label}>
      {items.map((item, i) => (
        <li key={`${item}-${i}`} className={`chip ${variant}`.trim()}>
          {item}
        </li>
      ))}
    </ul>
  );
}

export function ProfileReveal({ profile, country, answerCount, contextCount, usage, actions }: ProfileRevealProps) {
  const { taste } = profile;
  const colors = taste.colors.map((c) => ({ ...c, hex: safeColor(c.hex, "#141414") }));
  const basis =
    `Based on ${answerCount} picks` +
    (contextCount ? ` and ${contextCount} thing${contextCount === 1 ? "" : "s"} you shared` : "");

  return (
    <div className="j-profile stack-lg">
      {/* Poster card */}
      <article className="j-reveal card pop-in">
        {colors.length > 0 && (
          <div className="j-reveal__stripe" aria-hidden="true">
            {colors.map((c, i) => (
              <span key={`${c.hex}-${i}`} style={{ background: c.hex }} />
            ))}
          </div>
        )}
        <div className="j-reveal__body">
          <p className="eyebrow">
            <span aria-hidden="true">{flagEmoji(country.code)}</span> Your Tee:dentity
          </p>
          <h1 className="display j-reveal__archetype">{profile.archetype}</h1>
          {profile.tagline && <p className="j-reveal__tagline">{profile.tagline}</p>}
          <nav className="j-reveal__next" aria-labelledby="j-next-h">
            <p id="j-next-h" className="j-reveal__next-lead">
              Your profile is ready. Next:
            </p>
            <div className="j-reveal__next-ctas">
              <Link to="/shop" className="btn btn-primary btn-lg j-reveal__cta">
                <span className="j-reveal__cta-icon" aria-hidden="true">
                  👕
                </span>
                Find my t-shirts
              </Link>
              <Link to="/studio" className="btn btn-blue btn-lg j-reveal__cta">
                <span className="j-reveal__cta-icon" aria-hidden="true">
                  ✏️
                </span>
                Design my own
              </Link>
            </div>
          </nav>
          <div className="j-reveal__summary">
            {paragraphs(profile.summary).map((p, i) => (
              <p key={i}>{p}</p>
            ))}
          </div>
          <p className="tiny muted">{basis}.</p>
        </div>
      </article>

      <div className="j-profile__grid">
        {/* Traits */}
        <section className="card j-panel slide-up" aria-labelledby="j-traits-h">
          <h2 id="j-traits-h" className="j-panel__h">
            <span aria-hidden="true">📊</span> Your traits
          </h2>
          <ul className="j-traits">
            {profile.traits.map((t, i) => (
              <TraitBar key={`${t.name}-${i}`} trait={t} index={i} />
            ))}
          </ul>
        </section>

        {/* Taste */}
        <section className="card j-panel slide-up" aria-labelledby="j-taste-h" style={{ animationDelay: "80ms" }}>
          <h2 id="j-taste-h" className="j-panel__h">
            <span aria-hidden="true">🎨</span> Your taste
          </h2>
          {colors.length > 0 && (
            <ul className="j-swatches" aria-label="Color palette">
              {colors.map((c, i) => (
                <li key={`${c.hex}-${i}`} className="j-swatch" style={{ "--i": i } as CSSProperties}>
                  <span className="swatch" style={{ background: c.hex }} aria-hidden="true" />
                  <span className="j-swatch__name">{c.name}</span>
                  <span className="j-swatch__hex">{c.hex.toUpperCase()}</span>
                </li>
              ))}
            </ul>
          )}
          <dl className="j-taste">
            {taste.aesthetics.length > 0 && (
              <div>
                <dt>Aesthetics</dt>
                <dd>
                  <ChipList items={taste.aesthetics} variant="chip-dark" label="Aesthetics" />
                </dd>
              </div>
            )}
            {taste.motifs.length > 0 && (
              <div>
                <dt>Motifs</dt>
                <dd>
                  <ChipList items={taste.motifs} variant="chip-blue" label="Motifs" />
                </dd>
              </div>
            )}
            {taste.typography && (
              <div>
                <dt>Typography</dt>
                <dd className="j-taste__text">{taste.typography}</dd>
              </div>
            )}
            {taste.fits.length > 0 && (
              <div>
                <dt>Fits</dt>
                <dd>
                  <ChipList items={taste.fits} label="Fits" />
                </dd>
              </div>
            )}
            {taste.avoid.length > 0 && (
              <div>
                <dt>Not you</dt>
                <dd>
                  <ChipList items={taste.avoid} variant="j-chip-avoid" label="Avoid" />
                </dd>
              </div>
            )}
          </dl>
        </section>

        {/* Cultural roots */}
        <section className="card j-panel slide-up" aria-labelledby="j-roots-h" style={{ animationDelay: "160ms" }}>
          <h2 id="j-roots-h" className="j-panel__h">
            <span aria-hidden="true">{flagEmoji(country.code)}</span> Cultural roots
          </h2>
          {paragraphs(profile.cultural_roots).map((p, i) => (
            <p key={i} className="j-panel__text">
              {p}
            </p>
          ))}
        </section>

        {/* Values */}
        <section className="card j-panel slide-up" aria-labelledby="j-values-h" style={{ animationDelay: "240ms" }}>
          <h2 id="j-values-h" className="j-panel__h">
            <span aria-hidden="true">💎</span> What you value
          </h2>
          <ol className="j-values">
            {profile.values.map((v, i) => (
              <li key={`${v}-${i}`}>{v}</li>
            ))}
          </ol>
        </section>
      </div>

      {/* Brief + CTAs */}
      <section className="j-brief slide-up" aria-labelledby="j-brief-h">
        <div className="j-brief__text">
          <p id="j-brief-h" className="eyebrow">
            Your t-shirt brief
          </p>
          <p className="j-brief__body">{profile.tshirt_brief}</p>
        </div>
        <div className="j-brief__ctas">
          <Link to="/shop" className="btn btn-primary btn-lg">
            <span aria-hidden="true">👕</span> Find my t-shirts
          </Link>
          <Link to="/studio" className="btn btn-blue btn-lg">
            <span aria-hidden="true">✏️</span> Design my own
          </Link>
        </div>
      </section>

      {usage && (
        <aside className="j-usage card" aria-label="AI token usage for this session">
          <div>
            <p className="eyebrow">AI use in this session</p>
            <p className="tiny muted">Estimated with course Luna token rates; hosted web-search fees are not included.</p>
          </div>
          <dl className="j-usage__stats">
            <div><dt>Input tokens</dt><dd>{usage.input_tokens.toLocaleString()}</dd></div>
            <div><dt>Cached input</dt><dd>{usage.cached_input_tokens.toLocaleString()}</dd></div>
            <div><dt>Output tokens</dt><dd>{usage.output_tokens.toLocaleString()}</dd></div>
            <div><dt>Estimated cost</dt><dd>${usage.estimated_cost_usd.toFixed(6)}</dd></div>
          </dl>
        </aside>
      )}

      {actions && <div className="j-profile__actions">{actions}</div>}
    </div>
  );
}
