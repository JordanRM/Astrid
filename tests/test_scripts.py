import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OLD = '''# old elizaOS env
POSTGRES_URL="{db}"
OPENROUTER_API_KEY=sk-or-SECRETVALUE
OPENROUTER_SMALL_MODEL='sao10k/l3-euryale-70b'
#OPENROUTER_LARGE_MODEL=deepseek/deepseek-r1
TELEGRAM_BOT_TOKEN=123:SECRETTOKEN
MCP_API_KEY=ignored
'''


def make_env(tmp_path, text):
    old = tmp_path / "old.env"
    old.write_text(text)
    new = tmp_path / "new.env"
    r = subprocess.run([sys.executable, str(REPO / "scripts/make_env.py"), str(old), str(new)], capture_output=True, text=True)
    return r, new


def test_make_env_maps_keys_quietly_and_locks_permissions(tmp_path):
    r, new = make_env(tmp_path, OLD.format(db="postgresql://u:p@h:5432/postgres"))
    assert r.returncode == 0
    assert "SECRET" not in r.stdout + r.stderr and "postgresql://u:p" not in r.stdout
    lines = dict(l.split("=", 1) for l in new.read_text().splitlines())
    assert lines["DATABASE_URL"] == "postgresql://u:p@h:5432/postgres"
    assert lines["OPENROUTER_API_KEY"] == "sk-or-SECRETVALUE" and lines["TELEGRAM_BOT_TOKEN"] == "123:SECRETTOKEN"
    assert lines["INTERACTOR_MODEL"] == "sao10k/l3-euryale-70b"       # quotes stripped
    assert lines["THINKING_MODEL"] == "deepseek/deepseek-r1-0528" and lines["TIMEZONE"] == "America/Toronto"
    assert "MCP_API_KEY" not in lines
    assert stat.S_IMODE(os.stat(new).st_mode) == 0o600


def test_make_env_reports_missing_names_not_values(tmp_path):
    r, new = make_env(tmp_path, "OPENROUTER_API_KEY=sk-or-SECRETVALUE\n")
    assert r.returncode == 1 and not new.exists()
    assert "DATABASE_URL" in r.stderr and "TELEGRAM_BOT_TOKEN" in r.stderr and "SECRETVALUE" not in r.stderr


def run_setup(tmp_path, db_url, server, old_text=None):
    work = tmp_path / "astrid"
    shutil.copytree(REPO, work, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".env", ".pytest_cache"))
    old = tmp_path / "old.env"
    old.write_text(old_text or OLD.format(db=db_url))
    env = dict(os.environ, ASTRID_TEST="1", SKIP_SERVICE="1", OLD_ENV=str(old),
               TELEGRAM_API_BASE=server.url, OPENROUTER_BASE_URL=server.url)
    for k in ("DATABASE_URL", "POSTGRES_URL", "OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "INTERACTOR_MODEL"):
        env.pop(k, None)
    r = subprocess.run(["bash", str(work / "scripts/setup.sh")], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    return r, work


def good_server(server):
    def h(path, body):
        if path.endswith("/getMe"):
            return 200, {"ok": True, "result": {"username": "astrid_bot"}}
        return 200, {"choices": [{"message": {"content": "hi there"}}]}
    server.handler = h


def test_setup_end_to_end_with_working_services(tmp_path, db_url, server):
    good_server(server)
    r, work = run_setup(tmp_path, db_url, server)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "database: ok" in out and "telegram: ok (@astrid_bot)" in out and "reply model" in out and "ok" in out
    assert "SECRET" not in out
    assert (work / ".env").exists() and stat.S_IMODE(os.stat(work / ".env").st_mode) == 0o600
    # rerun keeps .env and is harmless
    r2 = subprocess.run(["bash", str(work / "scripts/setup.sh")], capture_output=True, text=True, cwd=tmp_path, timeout=120,
                        env=dict(os.environ, ASTRID_TEST="1", SKIP_SERVICE="1", TELEGRAM_API_BASE=server.url, OPENROUTER_BASE_URL=server.url))
    assert r2.returncode == 0 and ".env already exists" in r2.stdout


def test_setup_stops_with_clear_message_when_a_check_fails(tmp_path, db_url, server):
    server.handler = lambda p, b: (200, {"ok": False, "error_code": 401, "description": "Unauthorized"}) if p.endswith("/getMe") else (401, {})
    r, _ = run_setup(tmp_path, db_url, server)
    out = r.stdout + r.stderr
    assert r.returncode == 1
    assert "telegram: FAILED" in out and "reply model: FAILED" in out and "A check failed" in out
    assert "database: ok" in out


def test_setup_refuses_while_old_bot_runs(tmp_path, db_url, server):
    good_server(server)
    fake = tmp_path / "bun"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    p = subprocess.Popen(["bash", "-c", "exec -a 'bun run start --character x' sleep 30"])
    try:
        r, _ = run_setup(tmp_path, db_url, server)
    finally:
        p.kill()
    assert r.returncode == 1 and "old elizaOS bot is still running" in r.stdout
