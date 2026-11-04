"""python -m bench.models [words...]   list OpenRouter models, flagging the ones with no extra content moderation.

Run on the VPS (the sandbox can't reach OpenRouter). Examples:
  python -m bench.models                     models that are not moderated and cost under $3 per million output tokens
  python -m bench.models deepseek glm        only models whose id or name contains any of those words
  python -m bench.models --all               include moderated models too
"""
from __future__ import annotations

import argparse
import sys

from astrid.config import ConfigError, load_config
from astrid.net import session


def fetch(base_url: str, key: str) -> list[dict]:
    r = session().get(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("data") or []


def _price(m: dict, which: str) -> float | None:
    try:
        return float((m.get("pricing") or {}).get(which)) * 1_000_000
    except (TypeError, ValueError):
        return None


def rows(models: list[dict], words: list[str], include_moderated: bool = False, max_out: float = 3.0) -> list[dict]:
    out = []
    for m in models:
        mod = (m.get("top_provider") or {}).get("is_moderated")
        hay = (m.get("id", "") + " " + m.get("name", "")).lower()
        if words and not any(w.lower() in hay for w in words):
            continue
        if mod and not include_moderated:
            continue
        price_out = _price(m, "completion")
        if price_out is not None and price_out > max_out and not words:
            continue
        out.append({"id": m.get("id"), "in": _price(m, "prompt"), "out": price_out,
                    "ctx": (m.get("top_provider") or {}).get("context_length") or m.get("context_length"), "moderated": bool(mod)})
    return sorted(out, key=lambda r: r["id"] or "")


def render(items: list[dict]) -> str:
    if not items:
        return "no matching models"
    f = lambda v: "-" if v is None else f"{v:.2f}"
    lines = [f"{'model id':<52}{'$/M in':>8}{'$/M out':>9}{'context':>10}  moderated"]
    lines += [f"{r['id']:<52}{f(r['in']):>8}{f(r['out']):>9}{(r['ctx'] or 0):>10}  {'yes' if r['moderated'] else 'no'}" for r in items]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.models")
    ap.add_argument("words", nargs="*")
    ap.add_argument("--all", action="store_true", help="include moderated models")
    args = ap.parse_args()
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    items = rows(fetch(cfg.openrouter_base_url, cfg.openrouter_api_key), args.words, args.all)
    print(render(items))
    print(f"\n{len(items)} model(s). 'moderated: no' means OpenRouter adds no filter of its own; the model itself can still be restrained.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
