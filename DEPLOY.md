# Deploy outlook-reg on a new workstation

This project is split into **code** (in git) and **secrets + state** (never in git).
Do NOT copy `Profiles/`, `Results/`, `logs/`, Resin `*.db`, or real `config.json` /
`proxies.txt` from the old machine — rebuild them here.

## 0. Prerequisites
- Python 3.13+ with `pip`
- Docker Desktop (for Resin) or an already-running Resin you can point at
- Git

## 1. Get the code
```bash
git clone <your-private-repo-url> outlook-reg
cd outlook-reg
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Firefox for ruyipage
ruyipage uses its own patched Firefox build, not system Firefox. Either:
- copy the folder `AppData/Local/ruyipage/browsers/firefox-151.0a1-151-ruyi-win64/`
  from the old machine to the same path here, OR
- run `python -m ruyipage install` (if your ruyipage version supports it).
Then set the absolute path in your local `config.json` (`ruyipage.browser_path`).

## 3. Local config (NOT committed)
```bash
cp config.example.json config.json
cp proxies.example.txt proxies.txt
```
Edit `config.json`:
- `ruyipage.browser_path` → the Firefox path from step 2.
- Leave `proxy_residential_*` empty unless you really use that provider.

## 4. Resin proxy gateway
Do **not** copy Resin's `state.db` / `cache.db` (sqlite is machine/WAL-specific).
Start a fresh Resin and re-feed your upstream proxies.

```bash
mkdir resin && cd resin
cp ../docker-compose.example.yml docker-compose.yml

# set tokens for THIS shell (or use a .env / your secret store)
export RESIN_ADMIN_TOKEN="<new-admin-token>"
export RESIN_PROXY_TOKEN="<new-proxy-token>"

docker compose up -d
```
Feed your upstream proxy list (raw http://socks5://ip:port lines) into Resin:
```bash
python add_subscription.py path/to/your_upstream_proxies.txt
```
(`add_subscription.py` is in the repo's `resin/` notes; it just POSTs the list
to Resin which health-checks and filters them itself.)

## 5. Point proxies.txt at Resin
`proxies.txt` lines are `host:port:<platform>.<account>:<RESIN_PROXY_TOKEN>`:
```
127.0.0.1:2260:Default.s1:<RESIN_PROXY_TOKEN>
127.0.0.1:2260:Default.s2:<RESIN_PROXY_TOKEN>
...
```
The `Default` platform uses the full pool with a long sticky TTL — preferred
over region-filtered platforms (see project memory).

## 6. Run
```bash
python main.py
```

## What's intentionally not in git
`config.json`, `proxies.txt`, `Profiles/`, `Results/`, `logs/`, `__pycache__/`,
Resin `docker-compose.yml` (only the example is committed), Resin `data/*.db`.
Keep it that way — secrets and runtime state stay per-machine.
