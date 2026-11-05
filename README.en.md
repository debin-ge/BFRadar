# BFRadar (BlackFridayRadar)

English · [中文](README.md)

Black Friday / Cyber Monday VPS and hosting deals are scattered across forums and communities. BFRadar collects them from public RSS feeds, extracts **plan-level** fields from each post, and serves a filterable, comparable list in Chinese and English. MIT licensed.

Per deal it extracts: plan specs (CPU, RAM, disk, bandwidth, port speed, locations, network tags), prices in the original currency plus a CNY estimate, coupon codes, discount type (first term / recurring / lifetime) and end date. It covers four product types only — VPS, dedicated servers, web hosting and storage — and does not track stock. Every deal links back to the original post, and the site shows at most a 300-character excerpt.

## How it works

```
RSS feeds ─► source plugins ─► raw posts (SQLite) ─► classify ─► plan segmentation ─► rule-based extraction ─┐
                                                                                                             ▼
          JSON API · RSS · sitemap · share images ◄── merge + change detection ◄── optional LLM pass with anti-hallucination checks
                     ▲
                     └── React frontend (server-rendered; Chinese at /, English at /en/)
```

- **Rules first, LLM optional.** Prices, billing periods, specs and coupon codes are extracted deterministically. With an OpenAI-compatible endpoint configured, an LLM translates titles, writes summaries and fills gaps — but any price or code it returns must be found in the original post, otherwise it is discarded in favour of the rule result. Everything works without an LLM.
- **Change detection.** When a post reappears, fields are compared and "price drop" / "new code" flags are produced.
- **Split frontend/backend.** The Python (FastAPI) backend serves data only; the React Router + Express frontend is the site's single entry point.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12, pinned in `backend/.python-version`), Node 24 and pnpm.

```bash
# backend
cd backend
uv sync
cp .env.example .env                 # every variable is optional; blank means default
uv run python main.py init-db
uv run python main.py crawl          # one crawl round (--llm-limit 0 skips the LLM)
uv run python main.py serve          # backend + scheduler, API at http://127.0.0.1:8000/api/…

# frontend (another terminal)
cd frontend
pnpm install
API_ORIGIN=http://127.0.0.1:8000 pnpm dev     # http://127.0.0.1:3000
```

`uv run python main.py --help` lists the other commands (`stats`, `show`, `reprocess`, `llm-backfill`, `fx-update`, …).

### Configuration

Everything is configured through environment variables, or `backend/.env` (real environment variables win). Each option is documented in [`backend/.env.example`](backend/.env.example). The important ones:

- `SITE_URL`, `CONTACT_EMAIL`: required before going public. They go into the crawler's User-Agent so the sites you crawl know who you are and how to reach you.
- `SOURCES`, `INTERVAL_<SOURCE>`: which sources are enabled and how often each is fetched.
- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`: the LLM is enabled only when all three are set.
- `HTTPS_PROXY`: only if crawling must go through a proxy.

## Local deployment

Docker Compose runs it on your machine as two containers: `app` (backend + scheduler) and `web` (frontend, the single entry point). Only one port is opened, on `127.0.0.1`.

```bash
cp backend/.env.example backend/.env   # app configuration; CONTACT_EMAIL is recommended
docker compose up -d --build           # then open http://127.0.0.1:3000/
```

- The database and backups live in `backend/data/` and survive stops and rebuilds. A backend started for local development reads the same directory, so don't run both at once.
- Set `SITE_URL` in `backend/.env` to the address you open the site at (`http://127.0.0.1:3000` locally); the sitemap, RSS and share images use it for absolute URLs.
- Different port: `WEB_PORT=8080 docker compose up -d`.
- To crawl through a local proxy, set `HTTPS_PROXY=http://host.docker.internal:<proxy port>` in `backend/.env`.
- `docker compose logs -f app` shows the logs, `docker compose down` stops everything.

## Crawling principles and compliance

The crawler is written to these rules; keep them if you run your own instance, and take responsibility for it:

- **Public feeds only.** Only public RSS / Atom, no login, no browser emulation. A Cloudflare challenge makes the request fail and is recorded — it is never bypassed.
- **Identify yourself.** User-Agent is `Mozilla/5.0 (compatible; BFRadar/0.1; +SITE_URL; CONTACT_EMAIL)`; fill in your own.
- **Low frequency.** Default intervals are in `backend/.env.example`; `Retry-After` is honoured on 429 / 5xx.
- **Excerpts, not republication.** Pages and API expose structured fields, LLM-written summaries and ≤ 300-character excerpts, each linking to the original post; community threads are kept as titles and links only. Full text stays in the local database for extraction and is never served, and authors' usernames are not shown.
- **Post content belongs to its authors and sites.** The MIT license covers this repository's code and docs only, not any collected content.

Each source site has its own terms and `robots.txt`, and both change. Check them one by one before you deploy and decide for yourself whether your use is allowed. Usernames and contact details in posts may be personal data — don't publish or repurpose them — and honour takedown requests promptly.

## License

[MIT](LICENSE) © 2026 BFRadar contributors

