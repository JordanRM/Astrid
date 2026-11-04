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
Her everyday memories and reading are left out while it's on, and it ends as soon as you change the subject.
To give it its own model, set `HEATED_MODEL=<openrouter id>` in `.env`; empty means the normal reply model.

    .venv/bin/python -m bench.models                       # models with no extra OpenRouter moderation, under $3/M output
    .venv/bin/python -m bench.models deepseek minimax      # or search by name
    .venv/bin/python -m bench --models deepseek/deepseek-v4.1-flash,minimax/minimax-m2-her --scenarios heated --serve   # real ids, comma-separated, no spaces

## Her own reading

With `BRAVE_API_KEY` in `.env`, she reads on her own time: every 2 to 4 hours between 8am and midnight, plus once
between 1 and 5am. She skips a run while you're mid-conversation. Each run she picks a question from something you said,
one of her own views, a question she left herself, an open nudge, or (more often when things are close) something about
sex and desire. Then she searches with safe search off, reads a page or two and keeps a short thought. Thoughts stay
private unless they fit the conversation or you ask what she's been up to.

    .venv/bin/python -m astrid thoughts          # what she's read and made of it, with links
    .venv/bin/python -m astrid wonder            # send her off to read something now
    .venv/bin/python -m astrid wonder desire     # or force a kind: thread, opinion, follow, drift, desire, heat

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
