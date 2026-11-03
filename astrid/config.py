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
    history_limit: int = 24
    max_reply_tokens: int = 350
    temperature: float = 0.9
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    telegram_api_base: str = "https://api.telegram.org"
    appraisal_timeout: float = 150.0


def _first(env, *names):
    for n in names:
        v = (env.get(n) or "").strip()
        if v:
            return v
    return ""


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
    )
