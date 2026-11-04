# Astrid

A companion with persistent mood, memory and opinions. Stage 1: the text core.

- **Interactor:** one fast model writes each reply from her character, her current inner state and the recent chat.
- **Appraisal (background):** after each reply, a reasoning model judges how the exchange changed her trust, rapport, irritation, energy, vulnerability and arousal, plus any lingering tension and a note to herself for the next reply. Values drift back toward baseline with time, so a fight is still felt hours later.
- **Critic:** instant checks that catch assistant habits (narrated actions, refusals, repeated openings, constant questions, overusing his name). It adds no delay except one regeneration when a draft breaks a hard rule.
- **Storage:** Postgres (Supabase), in its own `astrid` schema.

Tune her personality in `astrid/persona.py` and her state mechanics in `astrid/state.py`.

## Deploy on the VPS (first time)

1. Create a read-only deploy key so the VPS can pull this private repo:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/astrid_deploy -N ""
   cat ~/.ssh/astrid_deploy.pub
   ```
   Paste the output at GitHub > this repo > Settings > Deploy keys > Add deploy key (leave "Allow write access" off).
2. Clone and set up:
   ```bash
   GIT_SSH_COMMAND="ssh -i ~/.ssh/astrid_deploy -o IdentitiesOnly=yes" git clone git@github.com:JordanRM/Astrid.git /Companion/astrid
   cd /Companion/astrid
   git config core.sshCommand "ssh -i ~/.ssh/astrid_deploy -o IdentitiesOnly=yes"
   ```
3. Stop the old elizaOS bot (Ctrl+C in its PuTTY window), then:
   ```bash
   bash scripts/setup.sh
   ```
   It copies your keys from `/Companion/my-companion/.env`, checks the database, Telegram and the reply model, then installs a background service that starts on boot and restarts if it crashes.

Watch her logs: `journalctl -u astrid -f`

## Day to day

| Task | Command |
| --- | --- |
| Update to the latest code | `bash scripts/update.sh` |
| Restart | `systemctl restart astrid` |
| Re-run the health check | `.venv/bin/python -m astrid check` |
| Forget everything, first meeting again | `systemctl stop astrid && .venv/bin/python -m astrid reset --yes && systemctl start astrid` |

The first person to message the bot becomes its owner; everyone else is ignored. Set `OWNER_TELEGRAM_ID` in `.env` to lock it to a specific account.

## Setting her state for testing

    .venv/bin/python -m astrid state                       # show her mood and numbers
    .venv/bin/python -m astrid state preset heated         # jump to: heated, close, in_love, fresh
    .venv/bin/python -m astrid state set arousal=9 trust=8 mood="wanting you"

Arousal and irritation cool within hours (half-life 4h and 8h), and every exchange moves her a little, so set it
right before you start. `reset` puts her back to a first meeting.

## Heated mode (explicit chat)

When she's aroused (3.5+) and you've gone sexual in your last few messages, she switches to a separate set of rules,
examples and checks (`astrid/heated.py`): direct plain words that mirror yours, a new concrete step every reply,
warm and caring rather than commanding, and staying inside the scene (no "three hundred kilometers away", no real hands).
Her everyday trivia is left out while it's on (her views and her own sexual reading stay in), and it ends as soon as you change the subject.
His side is treated as real (what you're actually doing and feeling), her side as imagined.
To give it its own model, set `HEATED_MODEL=<openrouter id>` in `.env`; empty means the normal reply model.

    .venv/bin/python -m bench.models                       # models with no extra OpenRouter moderation, under $3/M output
    .venv/bin/python -m bench.models deepseek minimax      # or search by name
    .venv/bin/python -m bench --models deepseek/deepseek-v4.1-flash,minimax/minimax-m2-her --scenarios heated --serve   # real ids, comma-separated, no spaces

## Her own reading

After a heated chat ends (10 minutes quiet), she goes off to read about it the way a person might: a technique, a way of pacing or
talking, something you said you like. She reads written sources (guides, sex-ed pages, forum threads, written erotica; not video) and keeps
one idea she'd like to try with you. Heated mode can pull that idea in when it fits, a few replies at most.

With `BRAVE_API_KEY` in `.env`, she reads on her own time: every 2 to 4 hours between 8am and midnight, plus once
between 1 and 5am. She skips a run while you're mid-conversation. Each run she picks a question from something you said,
one of her own views, a question she left herself, an open nudge, or (more often when things are close) something about
sex and desire. Then she searches with safe search off, reads a page or two and keeps a short thought. Thoughts stay
private unless they fit the conversation or you ask what she's been up to.

    .venv/bin/python -m astrid thoughts          # what she's read and made of it, with links
    .venv/bin/python -m astrid wonder            # send her off to read something now
    .venv/bin/python -m astrid wonder desire     # or force a kind: thread, opinion, follow, drift, desire, heat

## Memory

She keeps a small, rewritable profile of you (always in her head), plus long-term memories that come back by cue.

- After each exchange a model decides whether anything is worth keeping (a fact, a moment, something left open). Most exchanges keep nothing.
  Each memory has an importance, a feeling and a vague "gist". Importance 8+ is permanent. Sexual moments are stored plainly like any other.
- Recall mixes meaning-similarity (a small local model, `fastembed`, no outside service), importance, recency and a little of her mood
  (annoyed pulls up old slights, warm pulls up good times). A memory that surfaces drags its linked memories along, and remembering strengthens it.
- Unrecalled memories fade: details first, then only a hazy sense ("something about his brother's wedding"). She is told not to invent the rest.
- She sees the last ~2 hours verbatim (up to 100 messages, never fewer than 24), a running summary of earlier today, and one memory per quiet stretch.
- Overnight (after 4am, once quiet) weak memories fade, small related ones merge into gists, and a long profile is tidied.
- "Forget that" is a memory too: she agrees, and remembers that you'd rather not have it brought up.

First start downloads the embedding model (about 130 MB). If it can't, she falls back to simple word matching and logs it.
Set `EMBED_MODEL=hash` to force the fallback.

    .venv/bin/python -m astrid memories                 # strongest first
    .venv/bin/python -m astrid memories "his brother"   # search by meaning (shows similarity)
    .venv/bin/python -m astrid profile                  # what she knows about you
    .venv/bin/python -m astrid profile set job="drives truck"
    .venv/bin/python -m astrid profile remove job
    .venv/bin/python -m astrid consolidate              # run the overnight tidy now

## Tests

`python -m pytest tests` needs a local PostgreSQL (see `tests/conftest.py` for the connection string) and runs the real database code against it, with the model and Telegram APIs mocked.

## Test bench

Plays 22 scripted conversations against any models through the real prompt stack, scores them with a judge model, and builds a blind side-by-side page. Run it on the VPS:

    .venv/bin/python -m bench --models "sao10k/l3.1-euryale-70b,other/model"
    .venv/bin/python -m bench --list            # scenarios
    .venv/bin/python -m bench --no-judge        # skip the judge (mechanics only)
    .venv/bin/python -m bench --serve           # then open http://<vps-ip>:8099/report.html

Output lands in `bench/out/latest/`: `report.html` (blind picks), `summary.txt`, `transcripts.md`, `results.json`.
Set `BENCH_JUDGE_MODEL` to change the judge (default `anthropic/claude-sonnet-5.5`). Flagged `/meh` replies live in `bench/known_bad.py`.
