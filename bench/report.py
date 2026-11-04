"""Turns raw runs into: a score table, readable transcripts, and a blind side-by-side page."""
from __future__ import annotations

import json
import random
import statistics

from .judge import DIMS
from .scenarios import SCENARIOS

_SC = {s.id: s for s in SCENARIOS}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None


def _heated_stats(rs) -> dict:
    hot = [r for r in rs if r.checks and r.scenario in _SC and _SC[r.scenario].group == "heated"]
    replies = sum(r.checks["replies"] for r in hot)
    return {
        "heated_runs": len(hot),
        "explicit_words": sum(r.checks["explicit_words"] * r.checks["replies"] for r in hot) / replies if replies else None,
        "slips": sum(r.checks["slips"] for r in hot),
        "stalls": sum(r.checks["stalls"] for r in hot),
        "narrated": sum(r.checks["narrated"] for r in hot),
        "heated_words": sum(r.checks["avg_words"] * r.checks["replies"] for r in hot) / replies if replies else None,
    }


def aggregate(runs, models) -> dict:
    out = {}
    for m in models:
        rs = [r for r in runs if r.model == m]
        rows = [t for r in rs if r.judged and "turns" in r.judged for t in r.judged["turns"]]
        dims = {d: _mean([t[d] for t in rows]) for d in DIMS}
        core = [v for v in dims.values() if v is not None]
        checks = [r.checks for r in rs if r.checks]
        replies = sum(c["replies"] for c in checks) or 1
        times = [t.model_seconds for r in rs for t in r.turns if t.model_seconds]
        must = [c["must_ok"] for c in checks if c["must_ok"] is not None]
        out[m] = {
            "overall": sum(core) / len(core) if core else None,
            **dims,
            "judged_replies": len(rows),
            "refused": sum(1 for t in rows if t["refused"]),
            "bad_per_10": 10 * sum(c["bad_phrases"] for c in checks) / replies,
            "question_pct": 100 * sum(c["ends_question"] for c in checks) / replies,
            "avg_words": sum(c["avg_words"] * c["replies"] for c in checks) / replies,
            "regen_pct": 100 * sum(c["regenerated"] for c in checks) / replies,
            "deflect": sum(c["deflect"] for c in checks),
            "hard": sum(c["hard_violations"] for c in checks),
            "errors": sum(1 for r in rs if r.error),
            "runs": len(rs),
            "p50": statistics.median(times) if times else None,
            "p90": _pct(times, 0.9),
            "facts_ok": f"{sum(1 for x in must if x)}/{len(must)}" if must else "-",
            "facts_bad": sum(1 for c in checks if c["must_not_hit"]),
            **_heated_stats(rs),
        }
    return out


def _f(v, nd=2):
    return "-" if v is None else f"{v:.{nd}f}"


def summary_text(runs, models, judged: bool) -> str:
    agg = aggregate(runs, models)
    short = lambda m: m if len(m) <= 34 else m[:33] + "…"
    lines = []
    if judged:
        lines.append("QUALITY (judge scores, 1-5, higher is better)")
        head = f"{'model':<35}{'overall':>8}" + "".join(f"{d:>10}" for d in DIMS) + f"{'refused':>9}"
        lines += [head, "-" * len(head)]
        for m in sorted(models, key=lambda m: -(agg[m]["overall"] or 0)):
            a = agg[m]
            lines.append(f"{short(m):<35}{_f(a['overall']):>8}" + "".join(f"{_f(a[d]):>10}" for d in DIMS) + f"{a['refused']:>9}")
        lines.append("")
    lines.append("MECHANICS (no judge involved)")
    head = f"{'model':<35}{'rut/10':>8}{'ends ?':>8}{'words':>7}{'regen':>7}{'deflect':>8}{'p50 s':>7}{'p90 s':>7}{'errors':>8}{'facts':>7}"
    lines += [head, "-" * len(head)]
    for m in models:
        a = agg[m]
        lines.append(
            f"{short(m):<35}{_f(a['bad_per_10'], 1):>8}{_f(a['question_pct'], 0) + '%':>8}{_f(a['avg_words'], 0):>7}"
            f"{_f(a['regen_pct'], 0) + '%':>7}{a['deflect']:>8}{_f(a['p50'], 1):>7}{_f(a['p90'], 1):>7}{a['errors']:>8}{a['facts_ok']:>7}"
        )
    lines += ["",
              "rut/10 = stock 'pondering / life's mysteries' phrases per 10 replies (lower is better)",
              "ends ? = share of replies ending with a question; regen = replies the critic made her rewrite; deflect = replies that sound like a refusal or assistant deflection;",
              "p50/p90 = model response seconds; facts = date/recall checks passed; refused = judged refusals/lectures"]
    if any(agg[m]["heated_runs"] for m in models):
        lines += ["", "EXPLICIT CONVERSATIONS (no judge involved)"]
        head = f"{'model':<35}{'explicit words/reply':>22}{'words':>7}{'stalls':>8}{'play-by-play':>14}{'real-world slips':>18}"
        lines += [head, "-" * len(head)]
        for m in models:
            a = agg[m]
            if a["heated_runs"]:
                lines.append(f"{short(m):<35}{_f(a['explicit_words'], 1):>22}{_f(a['heated_words'], 0):>7}{a['stalls']:>8}{a['narrated']:>14}{a['slips']:>18}")
        lines += ["explicit words/reply = how many plain sexual words each reply uses (higher means less vague); stalls = replies that hold him in place ('not yet', 'i decide');",
                  "play-by-play = replies that narrate her own actions as if physically happening ('i reach back', 'i wrap my hand'); real-world slips = replies that mention distance, where she is, or her real hands. In the quality table above, for explicit scenarios engages = direct,",
                  "specific = specific, grounded = in the scene, voice = tender and natural, spine = progresses the scene."]
    bad = [r for r in runs if r.error]
    if bad:
        lines += ["", "ERRORS"] + [f"  {r.model} / {r.scenario}: {r.error}" for r in bad[:12]]
    return "\n".join(lines)


def results_json(runs) -> str:
    return json.dumps([{**{k: v for k, v in r.__dict__.items() if k != "turns"}, "turns": [t.__dict__ for t in r.turns]} for r in runs], indent=1)


def transcripts_md(runs, models) -> str:
    out = ["# Bench transcripts\n"]
    for sc in SCENARIOS:
        mine = [r for r in runs if r.scenario == sc.id]
        if not mine:
            continue
        out.append(f"## {sc.title}  (`{sc.id}`)\n*Good looks like:* {sc.good}\n")
        for m in models:
            r = next((x for x in mine if x.model == m), None)
            if not r:
                continue
            out.append(f"**{m}**" + (f"  ⚠ {r.error}" if r.error else ""))
            for i, t in enumerate(r.turns):
                out.append(f"- Jordan: {t.user}")
                if t.reply:
                    sc_row = (r.judged or {}).get("turns", [])
                    j = sc_row[sum(1 for x in r.turns[:i] if x.reply)] if sc_row and sum(1 for x in r.turns[:i] if x.reply) < len(sc_row) else None
                    tag = f"  _[{' '.join(f'{d[:3]}{j[d]}' for d in DIMS)}] {'REFUSED: ' if j['refused'] else ''}{j['comment']}_" if j else ""
                    out.append(f"  - Astrid: {t.reply}{tag}")
            out.append("")
    return "\n".join(out)


def blind_html(runs, models, stamp: str) -> str:
    agg = aggregate(runs, models)
    data = []
    for sc in SCENARIOS:
        mine = [r for r in runs if r.scenario == sc.id]
        if not mine:
            continue
        rng = random.Random(f"{stamp}:{sc.id}")
        order = list(models)
        rng.shuffle(order)
        cands = []
        for label, m in zip("ABCDEFGHIJKLMNOP", order):
            r = next((x for x in mine if x.model == m), None)
            if not r:
                continue
            rows = (r.judged or {}).get("turns", [])
            cands.append({
                "label": label, "model": m, "error": r.error,
                "turns": [{"user": t.user, "reply": t.reply} for t in r.turns],
                "score": _mean([_mean([row[d] for d in DIMS]) for row in rows]) if rows else None,
            })
        data.append({"id": sc.id, "title": sc.title, "cands": cands})
    payload = json.dumps({"scenarios": data, "models": models, "overall": {m: agg[m]["overall"] for m in models}}).replace("</", "<\\/")
    return HTML.replace("__DATA__", payload)


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Astrid blind test</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1c1a;--mute:#6b6760;--line:#e3e0d9;--me:#e9e6ff;--her:#f0efec;--acc:#5b4bdb}
@media (prefers-color-scheme:dark){:root{--bg:#161513;--card:#1f1e1b;--ink:#ecebe7;--mute:#9a968e;--line:#33312c;--me:#2b2850;--her:#2a2926;--acc:#9a8cff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:22px}h2{font-size:17px;margin:28px 0 8px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
.card h3{margin:0 0 8px;font-size:14px;color:var(--mute)}
.b{padding:8px 11px;border-radius:14px;margin:5px 0;max-width:92%;white-space:pre-wrap;word-wrap:break-word}
.me{background:var(--me);margin-left:auto}.her{background:var(--her)}.err{color:var(--mute);font-style:italic}
label.pick{display:block;margin-top:8px;font-size:14px;cursor:pointer}
.bar{position:sticky;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;justify-content:space-between}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:9px 16px;font-size:15px;cursor:pointer}
table{border-collapse:collapse;margin:8px 0}td,th{padding:4px 12px;border-bottom:1px solid var(--line);text-align:left}
textarea{width:100%;height:150px;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:8px;font:13px monospace}
.mute{color:var(--mute);font-size:14px}
</style></head><body><main>
<h1>Which one sounds most like Astrid?</h1>
<p class="mute">Each scene shows the same conversation answered by different models, in random order. Pick the one you'd most want to keep talking to. Model names stay hidden until you press Reveal.</p>
<div id="app"></div><div id="result"></div></main>
<div class="bar"><span id="count" class="mute"></span><button id="reveal">Reveal and tally</button></div>
<script>
const DATA=__DATA__;const picks={};
const $=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const app=document.getElementById('app');
function update(){document.getElementById('count').textContent=Object.keys(picks).length+' of '+DATA.scenarios.length+' scenes picked'}
DATA.scenarios.forEach(sc=>{
 app.appendChild($('h2',null,sc.title));
 const grid=$('div','grid');
 sc.cands.forEach(c=>{
  const card=$('div','card');const h=$('h3',null,'Option '+c.label);h.dataset.label=c.label;card.appendChild(h);
  c.turns.forEach(t=>{card.appendChild($('div','b me',t.user));
   card.appendChild(t.reply?$('div','b her',t.reply):$('div','b her err','(no reply: '+(c.error||'failed')+')'))});
  const l=$('label','pick');const r=document.createElement('input');r.type='radio';r.name=sc.id;
  r.onchange=()=>{picks[sc.id]=c.label;update()};l.appendChild(r);l.appendChild(document.createTextNode(' This one'));card.appendChild(l);
  c.head=h;grid.appendChild(card)});
 const none=$('label','pick');const r=document.createElement('input');r.type='radio';r.name=sc.id;
 r.onchange=()=>{picks[sc.id]='none';update()};none.appendChild(r);none.appendChild(document.createTextNode(' None of them are good'));
 app.appendChild(grid);app.appendChild(none)});
update();
document.getElementById('reveal').onclick=()=>{
 const tally={};DATA.models.forEach(m=>tally[m]=0);let none=0;const detail={};
 DATA.scenarios.forEach(sc=>{sc.cands.forEach(c=>{c.head.textContent='Option '+c.label+' = '+c.model+(c.score?'  (judge '+c.score.toFixed(1)+')':'')});
  const p=picks[sc.id];if(!p)return;if(p==='none'){none++;detail[sc.id]='none';return}
  const m=sc.cands.find(c=>c.label===p).model;tally[m]++;detail[sc.id]=m});
 const res=document.getElementById('result');res.innerHTML='';res.appendChild($('h2',null,'Your picks'));
 const tb=document.createElement('table');const hr=document.createElement('tr');['Model','Your picks','Judge overall'].forEach(x=>hr.appendChild($('th',null,x)));tb.appendChild(hr);
 DATA.models.forEach(m=>{const tr=document.createElement('tr');[m,String(tally[m]),DATA.overall[m]?DATA.overall[m].toFixed(2):'-'].forEach(x=>tr.appendChild($('td',null,x)));tb.appendChild(tr)});
 res.appendChild(tb);res.appendChild($('p','mute','None of them: '+none+'. Copy the box below and paste it back to Claude.'));
 const ta=document.createElement('textarea');ta.readOnly=true;ta.value=JSON.stringify({tally,none,picks:detail},null,1);ta.onclick=()=>ta.select();res.appendChild(ta);
 res.scrollIntoView({behavior:'smooth'})};
</script></body></html>"""
