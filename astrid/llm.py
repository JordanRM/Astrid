"""OpenRouter chat client."""
from __future__ import annotations

import asyncio
import logging
import re
import time

import requests

from .net import session

log = logging.getLogger("astrid.llm")


class LLMError(Exception):
    pass


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class OpenRouter:
    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai/api/v1", timeout: float = 60.0):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _post(self, payload: dict, timeout: float) -> str:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = session().post(
                    self._base + "/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._key}",
                        "Content-Type": "application/json",
                        "X-Title": "Astrid",
                    },
                    timeout=timeout,
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("error"):
                        raise LLMError(f"provider error: {str(data['error'])[:200]}")
                    choice = (data.get("choices") or [{}])[0]
                    content = (choice.get("message") or {}).get("content") or ""
                    content = _THINK.sub("", content).strip()
                    if not content:
                        if choice.get("finish_reason") == "length":
                            # It spent the whole budget thinking. The same request will do the same, so don't repeat it:
                            # the caller retries with more room instead.
                            raise LLMError("empty completion (ran out of tokens while thinking)")
                        raise LLMError("empty completion")
                    return content
                if r.status_code in (429, 500, 502, 503, 504):
                    last = LLMError(f"HTTP {r.status_code}")
                else:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
            except (requests.Timeout, requests.ConnectionError) as e:
                last = LLMError(f"network: {type(e).__name__}")
            except LLMError as e:
                # empty completions and provider hiccups are worth one more try; auth/4xx are not
                if str(e).startswith("empty completion (ran out"):
                    raise
                if str(e).startswith(("empty", "provider")):
                    last = e
                else:
                    raise
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        raise last or LLMError("unknown failure")

    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        extra: dict | None = None,
    ) -> str:
        payload: dict = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra:
            payload.update(extra)
        return await asyncio.to_thread(self._post, payload, timeout or self._timeout)
