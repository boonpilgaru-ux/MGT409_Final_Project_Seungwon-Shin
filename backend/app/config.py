"""Environment loading, model names, and the Portkey (OpenAI-compatible) client factory.

Secrets: the Portkey key is read from the environment only. It is never logged, printed, or
returned by any endpoint (``/api/health`` reports just whether it is set).

.env lookup order (first value wins; real environment variables beat every file):
  1. ``backend/.env``            (optional, local overrides)
  2. ``teedentity/.env``         (project-local configuration)
  3. the parent folder's ``.env``
  4. the legacy shared course ``.env`` two levels above the project
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
LEGACY_SHARED_ENV_FILE = PROJECT_DIR.parent.parent / ".env"

for _env_file in (
    BACKEND_DIR / ".env",
    PROJECT_DIR / ".env",
    PROJECT_DIR.parent / ".env",
    LEGACY_SHARED_ENV_FILE,
):
    if _env_file.is_file():
        load_dotenv(_env_file, override=False)


def _env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


# Where this app lives. Sent in our crawler User-Agents (context links, shop link checks, image
# search) because Wikimedia/Wikipedia answer 403 to bot UAs that carry no contact URL. Set it to
# the real project URL when deploying. Never put a personal email here.
PUBLIC_URL = _env("TEE_PUBLIC_URL", "http://localhost:5173")
COURSE_MODEL = "gpt-6-luna"


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings. Read via :func:`get_settings`."""

    portkey_base_url: str = field(default_factory=lambda: _env("PORTKEY_BASE_URL", "https://api.portkey.ai/v1"))
    question_model: str = COURSE_MODEL
    profile_model: str = COURSE_MODEL
    shop_model: str = COURSE_MODEL
    # The course key is restricted to Luna, so every app feature uses this fixed model.
    shop_backup_model: str = COURSE_MODEL
    design_model: str = COURSE_MODEL
    cheap_model: str = COURSE_MODEL
    db_path: Path = field(default_factory=lambda: Path(_env("TEE_DB_PATH", str(BACKEND_DIR / "data" / "app.db"))))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in _env("TEE_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
            if o.strip()
        )
    )
    llm_timeout_s: float = field(default_factory=lambda: float(_env("TEE_LLM_TIMEOUT", "90")))
    # Host headers the API answers (TrustedHostMiddleware, against DNS rebinding). Add the public
    # host name when deploying. Ports are ignored.
    allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip() for h in _env("TEE_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()
        )
    )
    # Sessions untouched this long are deleted at startup (0 = keep forever). Orders are kept.
    session_retention_days: float = field(default_factory=lambda: float(_env("TEE_SESSION_RETENTION_DAYS", "30")))

    @property
    def portkey_key_set(self) -> bool:
        return bool(os.getenv("PORTKEY_API_KEY", "").strip())

    def model_map(self) -> dict[str, str]:
        """Model names by role (safe to expose; contains no secrets)."""
        return {
            "question": self.question_model,
            "profile": self.profile_model,
            "shop": self.shop_model,
            "shop_backup": self.shop_backup_model,
            "design": self.design_model,
            "cheap": self.cheap_model,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def _portkey_kwargs() -> dict:
    settings = get_settings()
    return {
        "base_url": settings.portkey_base_url,
        "api_key": "portkey",  # placeholder; Portkey authenticates via the header below
        "default_headers": {
            "x-portkey-api-key": os.getenv("PORTKEY_API_KEY", "").strip(),
            # The gateway caches exact repeats (verified: a 3rd identical call came back in 0.1 s with
            # the same text). Every feature here wants fresh output ("regenerate", "refresh", a new
            # game per player), so always bypass cache reads.
            "x-portkey-cache-force-refresh": "true",
        },
        "timeout": settings.llm_timeout_s,
        "max_retries": 1,
    }


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """Shared synchronous OpenAI client pointed at Portkey."""
    return OpenAI(**_portkey_kwargs())


@lru_cache(maxsize=1)
def get_async_client() -> AsyncOpenAI:
    """Shared asynchronous OpenAI client pointed at Portkey."""
    return AsyncOpenAI(**_portkey_kwargs())
