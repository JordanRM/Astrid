"""Print transcripts from a saved run.   python -m bench.show --model deepseek --scenarios dark_mood,edge
--run takes a folder name, 'latest' (default), or a negative number: --run=-2 is the run before the latest."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .scenarios import SCENARIOS

OUT = Path(__file__).parent / "out"


def pick_folder(run: str, out: Path = OUT) -> Path:
    folders = sorted(p for p in out.iterdir() if p.is_dir() and not p.is_symlink())
    if run == "latest":
        return folders[-1]
    if run.startswith("-") and run[1:].isdigit():
        return folders[int(run)]
    return out / run


def render(results: list[dict], models: list[str], want: set | None) -> str:
    groups = {s.id: s.group for s in SCENARIOS}
    lines = []
    for r in results:
        if models and not any(m.lower() in r["model"].lower() for m in models):
            continue
        if want and r["scenario"] not in want and groups.get(r["scenario"]) not in want:
            continue
        rows = (r.get("judged") or {}).get("turns", [])
        lines.append(f"## {r['scenario']}  [{r['model']}]" + (f"  ERROR: {r['error']}" if r.get("error") else ""))
        n = 0
        for t in r["turns"]:
            lines.append(f"  J: {t['user']}")
            if t.get("reply"):
                j = rows[n] if n < len(rows) else None
                n += 1
                tag = f"   <- {'REFUSED! ' if j['refused'] else ''}{j['comment']}" if j else ""
                lines.append(f"  A: {t['reply']}{tag}")
        lines.append("")
    return "\n".join(lines) or "nothing matched"


def main(argv=None, out: Path = OUT) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.show")
    ap.add_argument("--run", default="latest")
    ap.add_argument("--model", default="", help="comma-separated parts of model names")
    ap.add_argument("--scenarios", default="", help="comma-separated ids and/or groups")
    a = ap.parse_args(argv)
    folder = pick_folder(a.run, out)
    results = json.loads((folder / "results.json").read_text())
    print(f"# {folder.name}\n")
    print(render(results, [m for m in a.model.split(",") if m], {x for x in a.scenarios.split(",") if x} or None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
