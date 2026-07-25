#!/usr/bin/env python3
"""Feed a proxy list file into Resin as a local subscription.

Usage:
  export RESIN_ADMIN_TOKEN=...
  python add_subscription.py <proxy_file> [subscription_name] [api_url]

Resin parses http://, socks5://, ip:port, ip:port:user:pass lines and
health-checks them itself — feed the raw list, let Resin filter dead ones.
Token comes from the RESIN_ADMIN_TOKEN env var (never hardcoded here).
"""
import json
import os
import sys
import urllib.request
import urllib.error

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ADMIN = os.environ.get("RESIN_ADMIN_TOKEN", "")
DEFAULT_API = "http://127.0.0.1:2260/api/v1/subscriptions"


def main():
    if len(sys.argv) < 2:
        print("usage: python add_subscription.py <proxy_file> [name] [api_url]", file=sys.stderr)
        sys.exit(2)
    if not ADMIN:
        print("error: set RESIN_ADMIN_TOKEN in the environment first", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(os.path.basename(path))[0]
    api = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_API
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    lines = [l for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
    print(f"feeding {len(lines)} non-empty lines as '{name}' -> {api}")
    body = json.dumps({
        "name": name,
        "source_type": "local",
        "content": content,
        "update_interval": "30m",
        "enabled": True,
    }).encode()
    req = urllib.request.Request(api, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {ADMIN}", "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        print("HTTP", r.status)
        print(r.read().decode()[:1000])
    except urllib.error.HTTPError as e:
        print("HTTPError", e.code)
        print(e.read().decode()[:1000])
        sys.exit(1)


if __name__ == "__main__":
    main()
