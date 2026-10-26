import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { polyfillCountryFlagEmojis } from "country-flag-emoji-polyfill";
import flagFontUrl from "country-flag-emoji-polyfill/dist/TwemojiCountryFlags.woff2?url";
import App from "./App";
import { SessionProvider } from "./SessionContext";
import "./styles.css";
import "./enhancements.css";

// Windows has no flag emoji; load a bundled flag-only font there (no-op on macOS/iOS/Android).
polyfillCountryFlagEmojis("Twemoji Country Flags", flagFontUrl);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <SessionProvider>
        <App />
      </SessionProvider>
    </BrowserRouter>
  </StrictMode>,
);
