"""python -m bench --models a,b,c   (run on the VPS, where the OpenRouter key lives)"""
from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import json
import os
import sys
import time
from pathlib import Path

from astrid.config import ConfigError, load_config
from astrid.llm import OpenRouter

from .report import blind_html, summary_text, transcripts_md
from .runner import run_matrix
from .scenarios import SCENARIOS

DEFAULT_JUDGE = "anthropic/claude-sonnet-5.5"


def _serve(folder: Path, port: int):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"\nServing the report at http://<your-vps-ip>:{port}/report.html  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m bench")
    ap.add_argument("--models", help="comma-separated OpenRouter model ids (default: your INTERACTOR_MODEL)")
    ap.add_argument("--scenarios", default="all", help="comma-separated scenario ids, or 'all'")
    ap.add_argument("--judge", default=os.environ.get("BENCH_JUDGE_MODEL", DEFAULT_JUDGE))
    ap.add_argument("--no-judge", action="store_true", help="skip the judge model (cheaper, mechanics only)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--serve", action="store_true", help="after the run, serve report.html over HTTP")
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()

    if args.list:
        for s in SCENARIOS:
            print(f"{s.id:<18} {s.title}")
        return 0
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    models = [m.strip() for m in (args.models or cfg.interactor_model).split(",") if m.strip()]
    want = None if args.scenarios == "all" else {s.strip() for s in args.scenarios.split(",")}
    scenarios = [s for s in SCENARIOS if want is None or s.id in want]
    if not scenarios:
        print("no matching scenarios; try --list", file=sys.stderr)
        return 2
    judge = None if args.no_judge else args.judge
    print(f"{len(models)} model(s) x {len(scenarios)} scenario(s); judge: {judge or 'off'}")

    def progress(n, total, model, sid, err):
        print(f"  [{n}/{total}] {model} / {sid}" + (f"  FAILED: {err}" if err else ""), flush=True)

    llm = OpenRouter(cfg.openrouter_api_key, cfg.openrouter_base_url)
    runs = asyncio.run(run_matrix(cfg, llm, models, scenarios, judge, args.concurrency, progress))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = Path(__file__).parent / "out" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    summary = summary_text(runs, models, judge is not None)
    (folder / "summary.txt").write_text(summary)
    (folder / "transcripts.md").write_text(transcripts_md(runs, models))
    (folder / "report.html").write_text(blind_html(runs, models, stamp))
    (folder / "results.json").write_text(json.dumps(
        [{**{k: v for k, v in r.__dict__.items() if k != "turns"}, "turns": [t.__dict__ for t in r.turns]} for r in runs], indent=1))
    latest = folder.parent / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(folder.name)
    print("\n" + summary + f"\n\nFiles: {folder}\n  report.html (blind side-by-side), transcripts.md, summary.txt, results.json")
    if args.serve:
        _serve(folder, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
