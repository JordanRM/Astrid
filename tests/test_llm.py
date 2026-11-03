import asyncio

import pytest

from astrid import llm as llm_mod
from astrid.llm import LLMError, OpenRouter


def ok(text):
    return 200, {"choices": [{"message": {"content": text}}]}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)


def chat(server, **kw):
    c = OpenRouter("sekret", server.url, timeout=5)
    return asyncio.run(c.chat("m/x", [{"role": "user", "content": "hi"}], **kw))


def test_success_sends_auth_model_and_params(server):
    server.handler = lambda p, b: ok("<think>plan</think>  hello  ")
    assert chat(server, temperature=0.7, max_tokens=50) == "hello"
    path, body, headers = server.requests[0]
    assert path == "/chat/completions" and body["model"] == "m/x"
    assert body["temperature"] == 0.7 and body["max_tokens"] == 50
    assert headers["Authorization"] == "Bearer sekret"


def test_retries_rate_limit_then_succeeds(server):
    seq = iter([(429, {}), (503, {}), ok("fine")])
    server.handler = lambda p, b: next(seq)
    assert chat(server) == "fine" and len(server.requests) == 3


def test_gives_up_after_three_tries(server):
    server.handler = lambda p, b: (500, {})
    with pytest.raises(LLMError):
        chat(server)
    assert len(server.requests) == 3


def test_auth_error_is_not_retried(server):
    server.handler = lambda p, b: (401, {"error": "bad key"})
    with pytest.raises(LLMError, match="401"):
        chat(server)
    assert len(server.requests) == 1


def test_empty_completion_retried_then_fails(server):
    server.handler = lambda p, b: ok("<think>only thoughts</think>")
    with pytest.raises(LLMError, match="empty"):
        chat(server)
    assert len(server.requests) == 3


def test_provider_error_in_200_body(server):
    seq = iter([(200, {"error": {"message": "upstream"}}), ok("recovered")])
    server.handler = lambda p, b: next(seq)
    assert chat(server) == "recovered"


def test_connection_refused_raises_llm_error():
    c = OpenRouter("k", "http://127.0.0.1:9", timeout=2)
    with pytest.raises(LLMError, match="network"):
        asyncio.run(c.chat("m", [{"role": "user", "content": "x"}]))
