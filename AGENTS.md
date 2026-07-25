# AGENTS.md — autonomous deployment of outlook-reg

This file tells an AI coding agent how to deploy outlook-reg on a fresh
workstation with **zero human intervention**, end to end. Follow it top to
bottom; each step has a concrete verify check. Do not skip verification.

> **Scope**: code checkout, Python env, Firefox, local config, and connecting
> to an **already-running Resin** proxy gateway. Resin itself is deployed
> elsewhere (out of scope for this run) — you only need its listen address and
> tokens, supplied as environment variables.

---

## Inputs the agent must have (from env / secret store)

| Variable | Meaning | Example |
|---|---|---|
| `RESIN_HOST` | host:port of the running Resin gateway | `192.168.1.50:2260` |
| `RESIN_PROXY_TOKEN` | shared proxy password (`<platform>.<account>` username) | `…` |
| `GIT_REPO` | private repo URL | `https://github.com/<owner>/outlook-reg.git` |
| `RUYIPAGE_FIREFOX_DIR` *(optional)* | path to a pre-staged patched-Firefox folder | `/opt/ruyipage/firefox` |

If any required variable is missing, **stop and ask** — do not guess tokens.

---

## Step 1 — Clone and create the Python environment
```bash
git clone "$GIT_REPO" outlook-reg
cd outlook-reg
python -m venv .venv
# Windows: .venv\Scripts\activate ; POSIX: source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```
**Verify**: `python -c "import ruyipage, faker, requests; print('deps ok')"` exits 0.

## Step 2 — Provision the ruyipage Firefox binary
ruyipage needs its own patched Firefox, not system Firefox.

Try in order:
1. If `RUYIPAGE_FIREFOX_DIR` is set and contains a `firefox` / `firefox.exe`, use it.
2. Else run `python -m ruyipage install` and capture the install path it prints.
3. Else copy the folder `firefox-151.0a1-151-ruyi-win64/` from a machine that
   already has it (old workstation), into a stable local path.

Set `FF_EXE` to the actual binary (`.../firefox/firefox.exe` on Windows,
`.../firefox/firefox` on POSIX).

**Verify**: `"$FF_EXE" --version` prints a version and exits 0.

## Step 3 — Generate local config (never committed)
```bash
cp config.example.json config.json
cp proxies.example.txt proxies.txt
```
Edit `config.json`:
- `ruyipage.browser_path` → absolute path to `FF_EXE` from Step 2.
- Leave `proxy_residential_*` empty (unused).

Edit `proxies.txt`: one line per concurrent session, format
`<RESIN_HOST>:<platform>.<account>:<RESIN_PROXY_TOKEN>`. Use the `Default`
platform (full pool, long sticky TTL). Generate N lines (N = desired
`concurrent_flows`, default 5 → use `Default.s1` … `Default.s5`):
```
192.168.1.50:2260:Default.s1:<RESIN_PROXY_TOKEN>
192.168.1.50:2260:Default.s2:<RESIN_PROXY_TOKEN>
192.168.1.50:2260:Default.s3:<RESIN_PROXY_TOKEN>
192.168.1.50:2260:Default.s4:<RESIN_PROXY_TOKEN>
192.168.1.50:2260:Default.s5:<RESIN_PROXY_TOKEN>
```

**Verify**: `python -c "import json; json.load(open('config.json'))"` exits 0,
and `proxies.txt` has ≥1 non-comment line with a real token (not `REPLACE_WITH…`).

## Step 4 — Smoke-test the proxy gateway
Confirm each proxy line authenticates through Resin (HTTP 200, not 407):
```bash
python - <<'PY'
import requests
ok = 0
for line in open("proxies.txt"):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    host, user, pwd = line.split(":", 2)
    px = f"http://{user}:{pwd}@{host}"
    try:
        r = requests.get("https://ipinfo.io/json",
                         proxies={"http": px, "https": px}, timeout=15)
        if r.status_code == 200:
            ok += 1
    except Exception:
        pass
print(f"proxy ok: {ok}")
raise SystemExit(0 if ok >= 1 else 1)
PY
```
**Verify**: prints `proxy ok: >=1` and exits 0. If 0, the Resin address/token
is wrong — recheck Step 3, do not proceed.

## Step 5 — Run the test suite
```bash
python -m pytest tests/ -q
```
**Verify**: all tests pass (no `FAILED`). A failure here means a code/env
mismatch — fix before running the real flow.

## Step 6 — Launch the registration worker
```bash
python main.py
```
Watch the first run for:
- `[Info: Proxy Pool] - ready=N/...` (proxies probed healthy)
- no repeated `首次启动连接失败` (Firefox launch loop — means Step 2 path is wrong)
- `[Success: Email Registration]` lines appearing

Stop with `Ctrl+C` (graceful).

---

## Troubleshooting map
| Symptom | Likely cause | Fix |
|---|---|---|
| `首次启动连接失败` loop | wrong `browser_path` / Firefox won't start | redo Step 2, verify `FF_EXE --version` |
| `proxy ok: 0` | bad Resin host/token or wrong username format | `host:2260:Default.s1:<token>` — recheck Step 3/4 |
| `407 Proxy Authentication` | wrong `RESIN_PROXY_TOKEN` | re-set token, regenerate proxies.txt |
| `proxy_unavailable` / `proxy_acquire_failed` | gateway unreachable from this box | confirm `RESIN_HOST` reachable (firewall/VPN) |
| `state=risk` / `帐户创建已被阻止` | upstream IP reputation (datacenter) | environmental; not a deploy bug — see project notes |
| pytest `FAILED` | code/env mismatch | read the failure, fix before Step 6 |

## Hard rules for the agent
- **Never** commit `config.json`, `proxies.txt`, `Profiles/`, `Results/`, `logs/`, or any token. They are gitignored — keep them that way.
- **Never** copy old Resin `*.db` / `Profiles/` from another machine. Rebuild fresh.
- Do not modify tracked source just to make it run on this box; machine differences belong in the untracked local config.
- If a verify step fails, stop at that step and fix it — do not push forward to later steps.
