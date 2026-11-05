"""Printing flagged replies (/meh) in full, so they can be pasted or attached without screenshots."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def format_flag(r: dict, tz: str = "America/Toronto") -> str:
    when = datetime.fromtimestamp(r["created_at"], ZoneInfo(tz)).strftime("%a %b %-d %-I:%M %p") if r.get("created_at") else "?"
    out = [f"=== flag #{r['id']}  {when}  mood: {r.get('mood')}", f"note: {r.get('note') or '-'}", "", "conversation (oldest first):"]
    for m in (r.get("context") or []):
        at = datetime.fromtimestamp(m["at"], ZoneInfo(tz)).strftime("%-I:%M %p") if m.get("at") else ""
        out.append(f"  [{at}] {'jordan' if m['role'] == 'user' else 'astrid'}: {m['content']}")
    if not r.get("context"):
        out.append(f"  jordan: {r.get('user_text')}\n  astrid: {r.get('reply')}")
    t = r.get("trace")
    if t:
        out += ["", f"how she was set up: {t.get('kind')} / {t.get('mode')} / {t.get('model')}"
                    + (f" / rewritten once because: {'; '.join(t['rewritten'])}" if t.get("rewritten") else "")]
        if t.get("profile"):
            out += ["what she knew about him:", t["profile"]]
        out += ["what she was shown right before replying:", t.get("note") or ""]
    out += ["", "flagged reply (all of it):", r["reply"], ""]
    return "\n".join(out)
