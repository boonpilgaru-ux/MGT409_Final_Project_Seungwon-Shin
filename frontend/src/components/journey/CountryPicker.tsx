// Searchable country combobox (ARIA 1.2 combobox + listbox popup) with flags.
// Keys: ↓/↑ move (wraps), PageDown/PageUp jump, Enter picks, Esc closes (then clears), Tab leaves.
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { COUNTRIES, searchCountries, type CountryInfo } from "../../countries";

interface CountryPickerProps {
  value: CountryInfo | null;
  onChange: (country: CountryInfo | null) => void;
  label?: string;
  hint?: ReactNode;
  disabled?: boolean;
  autoFocus?: boolean;
  /** Countries to offer (defaults to every country). */
  countries?: readonly CountryInfo[];
  placeholder?: string;
}

const PAGE = 8;

const fold = (s: string) =>
  s
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .trim();

/** Bold the first case/accent-insensitive occurrence of `query` in `name` (lengths match after NFD strip for Latin names). */
function highlight(name: string, query: string): ReactNode {
  const q = fold(query);
  if (!q) return name;
  const folded = fold(name);
  const at = folded.indexOf(q);
  if (at < 0 || folded.length !== name.length) return name;
  return (
    <>
      {name.slice(0, at)}
      <mark className="j-picker__mark">{name.slice(at, at + q.length)}</mark>
      {name.slice(at + q.length)}
    </>
  );
}

export function CountryPicker({
  value,
  onChange,
  label = "Your country",
  hint,
  disabled = false,
  autoFocus = false,
  countries = COUNTRIES,
  placeholder = `Search ${countries.length} countries…`,
}: CountryPickerProps) {
  const baseId = useId();
  const inputId = `${baseId}-input`;
  const listId = `${baseId}-list`;
  const hintId = `${baseId}-hint`;
  const optionId = (i: number) => `${baseId}-opt-${i}`;

  const [query, setQuery] = useState(value?.name ?? "");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const optionRefs = useRef<Array<HTMLLIElement | null>>([]);

  // Parent picked a country (e.g. quick-pick chip): mirror it in the text box.
  useEffect(() => {
    if (value) setQuery(value.name);
  }, [value]);

  // While the box shows the chosen name, browse the full list instead of filtering to one row.
  const filter = value && query === value.name ? "" : query;
  const results = useMemo(() => searchCountries(filter, countries.length, countries), [filter, countries]);
  const selectedIndex = value ? results.findIndex((c) => c.code === value.code) : -1;

  // Keep the active option visible inside the scrolling popup (without scrolling the page).
  useEffect(() => {
    const list = listRef.current;
    const el = optionRefs.current[active];
    if (!open || !list || !el) return;
    const top = el.offsetTop;
    const bottom = top + el.offsetHeight;
    if (top < list.scrollTop) list.scrollTop = top;
    else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
  }, [open, active, results]);

  const openList = (at?: number) => {
    setOpen(true);
    setActive(at ?? (selectedIndex >= 0 ? selectedIndex : 0));
  };

  const select = (country: CountryInfo) => {
    onChange(country);
    setQuery(country.name);
    setOpen(false);
  };

  const move = (delta: number, wrap: boolean) => {
    if (!results.length) return;
    setActive((i) => {
      const next = i + delta;
      if (wrap) return (next + results.length) % results.length;
      return Math.max(0, Math.min(results.length - 1, next));
    });
  };

  const onInput = (e: ChangeEvent<HTMLInputElement>) => {
    const next = e.target.value;
    setQuery(next);
    setOpen(true);
    setActive(0);
    if (value && next !== value.name) onChange(null);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        if (open) move(1, true);
        else openList();
        break;
      case "ArrowUp":
        e.preventDefault();
        if (open) move(-1, true);
        else openList(selectedIndex >= 0 ? selectedIndex : results.length - 1);
        break;
      case "PageDown":
        if (open) {
          e.preventDefault();
          move(PAGE, false);
        }
        break;
      case "PageUp":
        if (open) {
          e.preventDefault();
          move(-PAGE, false);
        }
        break;
      case "Enter":
        // Closed popup: let Enter submit the surrounding form.
        if (open && results[active]) {
          e.preventDefault();
          select(results[active]);
        }
        break;
      case "Escape":
        if (open) {
          e.preventDefault();
          setOpen(false);
          if (value) setQuery(value.name);
        } else if (query) {
          e.preventDefault();
          setQuery("");
          onChange(null);
        }
        break;
      case "Tab":
        setOpen(false);
        break;
    }
  };

  // Leaving the box: accept an unambiguous typed match, otherwise restore the current pick.
  const onBlur = () => {
    setOpen(false);
    if (value) {
      setQuery(value.name);
      return;
    }
    const q = fold(query);
    if (!q) return;
    const exact = results.find((c) => fold(c.name) === q || c.code.toLowerCase() === q);
    const only = results.length === 1 ? results[0] : undefined;
    const match = exact ?? only;
    if (match) select(match);
  };

  const clear = () => {
    setQuery("");
    onChange(null);
    setActive(0);
    setOpen(true);
    inputRef.current?.focus();
  };

  return (
    <div className={`j-picker${open ? " is-open" : ""}`}>
      <label className="label" htmlFor={inputId}>
        {label}
      </label>
      <div className="j-picker__control">
        <span className="j-picker__flag" aria-hidden="true">
          {value ? value.flag : "🌍"}
        </span>
        <input
          ref={inputRef}
          id={inputId}
          className="input j-picker__input"
          type="text"
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={open && results[active] ? optionId(active) : undefined}
          aria-describedby={hint ? hintId : undefined}
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          placeholder={placeholder}
          value={query}
          disabled={disabled}
          autoFocus={autoFocus}
          onChange={onInput}
          onKeyDown={onKeyDown}
          onClick={() => !open && openList()}
          onFocus={(e) => e.currentTarget.select()}
          onBlur={onBlur}
        />
        {query && !disabled && (
          <button
            type="button"
            className="j-picker__clear"
            aria-label="Clear country"
            onMouseDown={(e) => e.preventDefault()}
            onClick={clear}
          >
            ×
          </button>
        )}
        <span className="j-picker__caret" aria-hidden="true">
          ▾
        </span>

        {open && (
        <div className="j-picker__popup" onMouseDown={(e) => e.preventDefault()}>
          {results.length ? (
            <ul ref={listRef} id={listId} role="listbox" aria-label="Countries" className="j-picker__list">
              {results.map((c, i) => {
                const isActive = i === active;
                const isChosen = value?.code === c.code;
                return (
                  <li
                    key={c.code}
                    id={optionId(i)}
                    ref={(el) => {
                      optionRefs.current[i] = el;
                    }}
                    role="option"
                    aria-selected={isActive}
                    className={`j-picker__opt${isActive ? " is-active" : ""}${isChosen ? " is-chosen" : ""}`}
                    onMouseMove={() => !isActive && setActive(i)}
                    onClick={() => select(c)}
                  >
                    <span className="j-picker__opt-flag" aria-hidden="true">
                      {c.flag}
                    </span>
                    <span className="j-picker__opt-name">{highlight(c.name, filter)}</span>
                    {isChosen && (
                      <span className="j-picker__check" aria-label="(selected)">
                        ✓
                      </span>
                    )}
                    <span className="j-picker__opt-code">{c.code}</span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p id={listId} className="j-picker__empty">
              No match for “{query}”.{" "}
              {countries === COUNTRIES
                ? "Try the English name or a 2-letter code."
                : `Right now you can pick from ${countries.map((c) => c.name).join(", ")}.`}
            </p>
          )}
        </div>
        )}
      </div>

      <span className="visually-hidden" aria-live="polite">
        {open ? `${results.length} ${results.length === 1 ? "country" : "countries"} available.` : ""}
      </span>
      {hint && (
        <span id={hintId} className="hint">
          {hint}
        </span>
      )}
    </div>
  );
}
