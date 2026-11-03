import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

PG = os.environ.get("TEST_PG_ADMIN", "postgresql://postgres@127.0.0.1:5544/postgres")


@pytest.fixture()
def db_url():
    """A fresh empty database for each test."""
    import uuid
    name = "t_" + uuid.uuid4().hex[:10]
    subprocess.run(["psql", PG, "-qc", f"create database {name} encoding 'UTF8' template template0 lc_collate 'C' lc_ctype 'C'"], check=True)
    yield PG.rsplit("/", 1)[0] + "/" + name
    subprocess.run(["psql", PG, "-qc", f"drop database {name} with (force)"], check=False)


class FakeLLM:
    """Scripted stand-in for the OpenRouter client."""

    def __init__(self):
        self.calls = []
        self.scripts = {}  # model -> list of replies (str or Exception) or a callable

    def script(self, model, *replies):
        self.scripts[model] = list(replies)

    async def chat(self, model, messages, **kw):
        self.calls.append({"model": model, "messages": messages, **kw})
        s = self.scripts.get(model)
        out = s.pop(0) if s and len(s) > 1 else (s[0] if s else "ok")
        if callable(out):
            out = out(messages)
        if isinstance(out, Exception):
            raise out
        return out


class MockServer:
    """Tiny HTTP server whose behaviour a test sets with a handler function(path, body)->(status, obj)."""

    def __init__(self):
        self.requests = []
        self.handler = lambda path, body: (200, {})
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append((self.path, body, dict(self.headers)))
                status, obj = outer.handler(self.path, body)
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture()
def server():
    s = MockServer()
    yield s
    s.close()


def make_cfg(db_url, **over):
    from astrid.config import Config
    base = dict(
        database_url=db_url, openrouter_api_key="k", telegram_token="T",
        interactor_model="euryale", thinking_model="r1",
    )
    base.update(over)
    return Config(**base)
