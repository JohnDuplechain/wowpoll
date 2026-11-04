# WowPoll · wowpoll.com

Dead-simple polls for streamers. Type a question and options, hit **Create**, and paste the link in chat. Everyone gets one vote.

## Features
- No accounts. Creating a poll takes about 5 seconds, and the link is copied automatically.
- One vote per person, enforced by an anonymous browser cookie. Optionally also one vote per network/IP (on by default).
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

## Deploy
Any host that runs Docker or Python works (Fly.io, Railway, Render, a VPS).

Environment variables:
| Var | Purpose |
|---|---|
| `SECRET_KEY` | **Set this in production.** Used to hash IPs. If it changes, the IP check stops recognizing earlier voters. |
| `DB_PATH` | SQLite file path. Put it on a persistent volume (`/data/polls.db` in Docker). |
| `TRUST_PROXY` | `1` when behind a proxy/load balancer so the real client IP is used (already set in the Dockerfile). |

Run as a **single instance** with a persistent disk, because SQLite plus the in-process lock assumes one process.
