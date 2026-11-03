"""Build Astrid's .env from the old elizaOS .env. Never prints secret values."""
import os
import sys

old_path, new_path = sys.argv[1], sys.argv[2]


def parse(path):
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    return out


old = parse(old_path)
mapping = {
    "DATABASE_URL": old.get("POSTGRES_URL") or old.get("DATABASE_URL", ""),
    "OPENROUTER_API_KEY": old.get("OPENROUTER_API_KEY", ""),
    "TELEGRAM_BOT_TOKEN": old.get("TELEGRAM_BOT_TOKEN", ""),
    "INTERACTOR_MODEL": old.get("INTERACTOR_MODEL") or old.get("OPENROUTER_SMALL_MODEL", ""),
    "THINKING_MODEL": old.get("THINKING_MODEL") or "deepseek/deepseek-r1-0528",
    "TIMEZONE": "America/Toronto",
}
missing = [k for k, v in mapping.items() if not v]
if missing:
    print("could not find these in the old .env: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
fd = os.open(new_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    for k, v in mapping.items():
        f.write(f"{k}={v}\n")
print("wrote " + new_path + " with: " + ", ".join(mapping))
