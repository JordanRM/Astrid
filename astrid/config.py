from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    database_url: str
    openrouter_api_key: str
    telegram_token: str
    interactor_model: str
    thinking_model: str
    owner_telegram_id: int | None = None
    timezone: str = "America/Toronto"
    history_limit: int = 24         # the smallest recent window she always sees
    history_minutes: int = 120      # she sees everything from roughly the last two hours...
    history_max: int = 100          # ...up to this many messages
    media_domains: tuple = ()        # sites she may take links from, e.g. MEDIA_DOMAINS=site1.com,site2.com; empty turns links off
    first_meeting_note: bool = True  # False only in the bench, for scenarios that aren't about meeting for the first time
    media_force: bool = False       # MEDIA_FORCE=on, for testing only: she always offers a stocked link in a heated moment (no odds, spacing or daily cap)
    reply_reasoning: bool = True    # REPLY_REASONING=off asks the reply model not to think first (faster, cheaper; test with the bench)
    outreach_speedup: float = 1.0   # testing only: OUTREACH_SPEEDUP=12 makes her waits and silences count 12x faster (1 = real time)
    outreach: bool = True           # she may text first; OUTREACH=off turns it off
    embed_model: str = ""           # empty: BAAI/bge-small-en-v1.5; "hash" forces the simple word-matching fallback
    max_reply_tokens: int = 600
    temperature: float = 0.9
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    telegram_api_base: str = "https://api.telegram.org"
    appraisal_timeout: float = 150.0
    appraisal_max_tokens: int = 2500
    provider_sort: str = "latency"  # OpenRouter routes to the fastest host; "" turns it off
    brave_api_key: str = ""         # enables her own reading; empty turns it off
    heated_model: str = ""          # reply model while things are sexual; empty means the normal reply model
    heated_max_tokens: int = 500
    heated_temperature: float = 1.0


def _speedup(raw: str) -> float:
    try:
        return max(1.0, min(30.0, float(raw))) if raw else 1.0
    except ValueError:
        return 1.0


def _first(env, *names):
    for n in names:
        v = (env.get(n) or "").strip()
        if v:
            return v
    return ""


def _provider_sort(env) -> str:
    v = _first(env, "INTERACTOR_PROVIDER_SORT").lower()
    if v in ("off", "none", "false", "0"):
        return ""
    return v or "latency"


def load_config(env=None) -> Config:
    if env is None:
        load_dotenv()
        env = os.environ
    values = {
        "DATABASE_URL": _first(env, "DATABASE_URL", "POSTGRES_URL"),
        "OPENROUTER_API_KEY": _first(env, "OPENROUTER_API_KEY"),
        "TELEGRAM_BOT_TOKEN": _first(env, "TELEGRAM_BOT_TOKEN"),
        "INTERACTOR_MODEL": _first(env, "INTERACTOR_MODEL", "OPENROUTER_SMALL_MODEL"),
    }
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise ConfigError("Missing required settings: " + ", ".join(missing))
    owner = _first(env, "OWNER_TELEGRAM_ID")
    try:
        owner_id = int(owner) if owner else None
    except ValueError:
        raise ConfigError("OWNER_TELEGRAM_ID must be a number")
    return Config(
        database_url=values["DATABASE_URL"],
        openrouter_api_key=values["OPENROUTER_API_KEY"],
        telegram_token=values["TELEGRAM_BOT_TOKEN"],
        interactor_model=values["INTERACTOR_MODEL"],
        thinking_model=_first(env, "THINKING_MODEL") or "deepseek/deepseek-r1-0528",
        owner_telegram_id=owner_id,
        timezone=_first(env, "TIMEZONE") or "America/Toronto",
        openrouter_base_url=_first(env, "OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",
        telegram_api_base=_first(env, "TELEGRAM_API_BASE") or "https://api.telegram.org",
        provider_sort=_provider_sort(env),
        brave_api_key=_first(env, "BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"),
        heated_model=_first(env, "HEATED_MODEL"),
        embed_model=_first(env, "EMBED_MODEL"),
        media_domains=tuple(d.strip().lower().removeprefix("www.") for d in _first(env, "MEDIA_DOMAINS").split(",") if d.strip()),
        media_force=_first(env, "MEDIA_FORCE").lower() in ("on", "true", "1", "yes"),
        reply_reasoning=_first(env, "REPLY_REASONING").lower() not in ("off", "false", "0", "no"),
        outreach_speedup=_speedup(_first(env, "OUTREACH_SPEEDUP")),
        outreach=_first(env, "OUTREACH").lower() not in ("off", "false", "0", "no"),
    )
