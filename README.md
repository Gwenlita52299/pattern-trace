# PatternTrace

**English** | [中文](README.zh-CN.md)

On-chain fund-flow tracing and money-laundering pattern detection for Bitcoin addresses. Given any
BTC address, the system builds a bounded transaction subgraph rooted at that address, runs structural
+ semantic hybrid retrieval against confirmed laundering patterns in the knowledge base, and has an
LLM produce an evidence-cited four-tier risk verdict (**high / medium / low / no_match**), with case
management and report export.

Built for on-chain risk investigation, and **self-hostable**: source + `docker compose` brings up every
service on your own host, with no dependency on any third-party cloud platform.

## Demo

**Enter a BTC address and pick the hop depth**

![PatternTrace home page: address input](docs/images/demo-home.png)

**Subgraph, four-tier verdict, evidence list and retrieval explanation**

![PatternTrace analysis page: subgraph, verdict and evidence](docs/images/demo-analyze.png)

## Capabilities

| Capability | Description |
|---|---|
| Subgraph construction | BFS with three queues + five termination conditions (unspent / time window / depth ≤ 3 / size pruning / early-stop); live mode adds retry backoff, circuit-breaking failover to a fallback endpoint, and Redis caching |
| Knowledge base | Lazarus confirmed cases split into subgraphs (~9,300 patterns), each with a Graphormer pooled vector `vector(784)` (HNSW index) + retrieval-fingerprint JSONB (WL multiset / amounts / receipts / UTXO / count vectors); negatives live in a separate table and are never recalled |
| Hybrid retrieval | Graphormer pooled cosine recall → three-channel re-rank `0.1·cos + 0.1·wljac + 0.8·ov` → Top-K |
| LLM judgment | DeepSeek cloud by default; OpenAI-compatible / local llama.cpp switchable; mock for offline demos. Structured JSON output; evidence cites address nodes only (`addr:*`); invalid citations are retried by anti-hallucination validation and then fail |
| Analysis pipeline | Four-stage progress (BFS build → hybrid retrieval → WL kernel re-rank → LLM judgment); the worker reports stages via Redis, the UI advances stage by stage, and failures are attributed to a specific stage |
| Visualization | React Flow layered canvas, click an evidence item to highlight the full path (node + adjacent edges + neighbors), node collapse / expand, dual-encoded amber box for mixers |
| Business loop | Case CRUD, address linking, async PDF / HTML reports (HMAC-signed download), audit log |
| Admin config | Admins can switch LLM provider / model / API key from the Settings page (encrypted at rest, effective immediately) without editing env vars or restarting |

## Architecture

```
frontend (Next.js + React Flow)      backend (FastAPI)            workers (arq)
        │  /api/v1 (Next rewrites, same-origin proxy) │             │
        ├──────────────────────────────► │  JWT access+refresh      │
        │                                ├──────────────────────────┤
        │                          PostgreSQL (pgvector)   Redis (queue/cache)
                                         LLM API (DeepSeek / OpenAI-compatible / local llama.cpp)
```

The browser talks only to the frontend on the same origin (`next.config.mjs` rewrites forward to the
backend); the refresh cookie stays same-site, and the session survives a full page reload.

- `backend/graph_builder/`: subgraph construction core (BFS three queues + five termination conditions, global ID scheme `addr:*` / `tx:*` / `edge:*`)
- `backend/detection/`: runtime CoinJoin / cross-chain OP_RETURN detection (modular protocol detection)
- `backend/retrieval/`: Graphormer vector recall + three-channel fingerprint re-rank
- `backend/llm_judge/`: provider abstraction (deepseek / OpenAI-compatible / mock) and anti-hallucination structured judgment
- `backend/services/`: analysis orchestration, report generation, seed cases, provider config
- `workers/worker.py`: arq entrypoint (runs analysis tasks)
- `ingest/`: case subgraph splitting, positive/negative sample generation, embedding computation (one-off KB build)
- `tests/`: unit / contract / E2E / performance tests

## Quick start

Prerequisites: Docker, plus `JWT_SECRET` and a bootstrap admin password in `.env` (see `.env.example`).

```bash
docker compose up --build          # db / redis / backend / worker / frontend
open http://localhost:3000         # frontend
open http://localhost:8000/docs    # API docs
```

Migrations run automatically in the backend container; the bootstrap admin is seeded on first start.
The default provider is DeepSeek cloud — set `LLM_API_KEY` in `.env`. The offline demo needs no key
(see below). Knowledge-base vectors and retrieval query data ship with the repo
(`ingest/seed/graphormer_v2/`), so a fresh clone works out of the box.

### Offline demo (no internet, no real model)

```bash
# mock provider with three seed cases (high / low / no_match); verdicts in seconds
LLM_PROVIDER=mock GRAPH_DATA_MODE=fixture python -m backend.services.seed_cases
```

### Local inference (optional): llama.cpp

For judgment without a cloud API, use llama.cpp (`--profile local-llm`):

```bash
docker compose --profile local-llm up --build
```

- Model: `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M` (~16GB), downloaded from HuggingFace on first start — allow disk and time
- Memory: the Docker Desktop VM needs ≥20GB (weights + KV cache + other containers share one memory pool)
- Switch backend/worker to `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL=http://llamacpp:8080/v1`, and keep `LLM_MODEL` identical to the llama-server `--alias` (the verdict cache key includes the model, so both sides must match)

### Backend-only dev environment

```bash
uv sync --extra dev
docker compose up -d db redis
uv run alembic upgrade head
JWT_SECRET=dev-secret uv run uvicorn backend.api.app:create_app --factory --reload
```

## Configuration

Environment variables are injected via `.env` or compose; full defaults live in `backend/core/config.py`:

| Variable | Description |
|---|---|
| `JWT_SECRET` | **Required**; length and entropy are validated at runtime, placeholders are rejected |
| `SECRETS_KEY` | Encryption key for provider API keys saved from the admin Settings page (`openssl rand -base64 32`). Without it the panel cannot store keys (env-based keys still work) |
| `BOOTSTRAP_ADMIN_EMAIL/PASSWORD` | First admin account; empty means no seeding |
| `LLM_PROVIDER` | `deepseek` (default, recommended for production) / OpenAI-compatible (incl. local inference) / `mock` (testing and demos) |
| `LLM_MODEL` | Defaults to `deepseek-chat`; for local inference, the llama-server `--alias` (e.g. `qwen3.8-27b`) |
| `LLM_BASE_URL` | Defaults to `https://api.deepseek.com`; local inference uses `http://llamacpp:8080/v1` |
| `LLM_API_KEY` | Cloud provider key (can also be saved on the admin Settings page, encrypted at rest) |
| `GRAPH_DATA_MODE` | `fixture` (built-in demo graph, offline) / `live` (Esplora public API) |
| `ESPLORA_API_URL` | Live data source, defaults to `https://mempool.space/api` (auto-fallback to Blockstream) |
| `ADDRESS_TX_COUNT_LIMIT` | Live-mode activity precheck threshold (default 200): addresses above it are rejected outright, keeping hyperactive addresses from blowing up graph construction |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | Outbound HTTP proxy; required for the live data source and LLM API on restricted networks |
| `DEMO_SEEDS` | CSV of allowlisted addresses usable without login; empty falls back to the built-in seed |
| `CORS_ORIGINS` | Explicit CORS allowlist CSV, defaults to `localhost:3000`; inject the real domain in production |
| `API_PROXY_URL` | Injected at frontend build time: the rewrite target (inside compose, `http://backend:8000`) |
| `COOKIE_SECURE` | Set `true` in production (cookie gets Secure + SameSite=None, enabling split frontend/backend domains) |

## Development and testing

```bash
uv sync --extra dev
bash infra/run_unit_tests.sh        # backend unit + contract tests (creates an isolated patterntrace_test DB)
cd frontend && npm test             # frontend vitest
cd frontend && npm run type-check && npm run lint

python -m scripts.gen_api_types     # sync the frontend contract after an OpenAPI schema change
```

**Backend tests must go through `infra/run_unit_tests.sh`**: the suite contains full-table cleanup,
while the default `DATABASE_URL` points at the dev database (the same Postgres your local stack uses)
— a plain `uv run pytest` would wipe the analyses you are viewing. This rule is enforced in code by
`scripts/db_guard.py`: destructive tests may only run against a database named `patterntrace_test` /
`pt_e2e` / `*_test`; anything else fails loudly.

CI (GitHub Actions) runs on PRs: backend lint / tests / contract guard, frontend lint + type-check +
vitest + build, and one full E2E path (analyze → judge → report for real, including a dedicated worker
queue shape). CI is a validation gate only; the deliverable is the docker compose self-hosting setup below.

## Deployment

This project is **self-hostable**: source + one `docker compose` orchestration brings up every service
on your own host.

```bash
git clone <repo> && cd pattern_trace
cp .env.example .env          # fill in JWT_SECRET / LLM_API_KEY / data source, etc.
docker compose up --build     # db / redis / backend / worker / frontend
open http://localhost:3000
```

- **Migrations run automatically**: the backend container runs `alembic upgrade head` on start, no extra step.
- **Production shape**: live data source + DeepSeek cloud judgment; add `--profile local-llm` for local
  inference (~16GB model on first start, ≥20GB memory).
- Production hardening (`COOKIE_SECURE`, `CORS_ORIGINS`, reverse proxy, etc.) is injected via `.env`;
  `docker compose` is arranged as needed and tied to no platform.

## Security baseline

- Password hashing bcrypt cost 12 (legacy pbkdf2 hashes still verify, smooth migration)
- JWT access (15min) + refresh rotation (reuse detection; state rotated atomically in Redis, in-process
  fallback when Redis is down); refresh tokens are **only ever set as an HttpOnly cookie, never returned
  in the response body**
- Write requests require `X-Requested-With` (CSRF enforced server-side, not left to the frontend)
- Explicit CORS allowlist: wildcards are rejected when `allow_credentials=True`
- Horizontal privilege isolation: other users' cases / reports always return 404 and are audit-logged
- Report download URLs: HMAC-SHA256 signed, 15-minute expiry
- Admin provider keys: encrypted at rest with Fernet; the API never echoes plaintext, only `key_source` / `has_key`
- Production (`COOKIE_SECURE=true`): cookie gets Secure + SameSite=None, supporting split-domain deployment

## License

[MIT](LICENSE)
