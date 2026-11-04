# WowPoll · wowpoll.com

Dead-simple polls for streamers. Type a question and options, hit **Create**, and paste the link in chat. Everyone gets one vote.

## Features
- No accounts. Creating a poll takes about 5 seconds, and the link is copied automatically.
- One vote per person, enforced by an anonymous browser cookie. The backend still supports an optional one-vote-per-IP mode (`strict_ip: true` in the API), but the UI doesn't offer it.
- Results update live every 2-3 seconds.
- **OBS overlay**: `/p/<id>/overlay` has a transparent background for a Browser Source. Add `?bg=1` for a solid panel.
- The creator can close or reopen voting from the poll page. The admin token is kept in their browser.
- IPs are stored only as salted HMAC hashes.

## Run locally
```
pip install -r requirements.txt
python -m uvicorn app:app --reload
```
Then open http://localhost:8000

Without a database URL the app uses a local SQLite file. To develop against Neon Postgres instead, run `neon env pull`. It writes `DATABASE_URL` and `DATABASE_URL_UNPOOLED` to the git-ignored `.env.local`, which the app reads on startup. `neon.ts` and `package.json` are for the Neon CLI only; the app itself is Python.

## Deploy (FastAPI Cloud)
The app is hosted on FastAPI Cloud, connected to this repo. FastAPI Cloud runs several replicas and has no persistent disk, so production **must** use Postgres:

1. In the app's dashboard, open **Integrations** and connect **Neon**. This sets `DATABASE_URL`.
2. Redeploy. `/healthz` should then report `"db": "postgres"`.

Tables and the IP-hashing secret are created automatically on first boot. Vote limits are enforced by database constraints, so they hold across all replicas.

Environment variables:
| Var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string. Without it the app uses a local SQLite file (`DB_PATH`), which is for development only. |
| `TRUST_PROXY_CHAIN` | Finds the real client IP by reading `X-Forwarded-For` from the right and skipping private and Cloudflare addresses, so faked entries are ignored. On by default on FastAPI Cloud. |
| `PROXY_HOPS` | Optional override: take the client IP from the n-th `X-Forwarded-For` entry counted from the right. |
| `DEBUG_IP` | Set to `1` temporarily to enable `/api/debug/ip`, which shows the IP the app sees. Use it to check `PROXY_HOPS`. |
| `SECRET_KEY` | Optional override for the IP-hashing secret (one is otherwise stored in the database). |
| `DB_POOL_SIZE` | Postgres connections per replica (default 10). |

A Dockerfile is also included for hosts like Fly.io, Railway or Render.
