# Seat Reservation at Scale

JSON HTTP API for assigned seats. Python 3.12+ standard library and SQLite; no package installation is needed. A reservation is confirmed immediately. Its owner can cancel it and release the seats.

## Run

```sh
docker compose up --build
```

Or run locally with `python3 app.py`. Data is stored at `data/reservations.db` locally and on a named `/data` volume in Docker Compose. **Deploy one service process with one persistent volume.** Do not run replicas against independent SQLite files.

`GET /health/live` checks the process. `GET /health/ready` opens the database and queries the shows table; it returns 503 when that fails. `GET /metrics` exposes Prometheus text. Every request emits a JSON log line to stdout with `request_id`, status, reason, and duration. Send an `X-Request-ID` header to correlate a request.

## Authentication for this exercise

User bearer tokens are HMAC-signed user IDs. The user ID in a request body is ignored. Generate a token with:

```sh
python3 app.py token alice
```

Set `AUTH_SECRET` to the same value for the server and token generation. `POST /shows` requires `Authorization: Bearer $ADMIN_TOKEN`. Defaults (`development-secret-change-me` and `development-admin-token`) are provided for exercise evaluation only. A real deployment should set its own values and provide test credentials to reviewers separately. This token mechanism demonstrates token-derived identity; production would use a trusted identity provider and key rotation.

## API

```sh
BASE=http://localhost:8000
TOKEN=$(python3 app.py token alice)

curl -sS -X POST "$BASE/shows" \
  -H 'Authorization: Bearer development-admin-token' \
  -H 'Content-Type: application/json' \
  -d '{"name":"friday-night","seats":["A1","A2","A3"],"price_paise":25000,"per_user_limit":4}'

curl -sS -X POST "$BASE/shows/SHOW_ID/reserve" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"seats":["A1"],"idempotency_key":"alice-first-attempt"}'

curl -sS "$BASE/shows/SHOW_ID"
curl -sS -X POST "$BASE/reservations/RESERVATION_ID/cancel" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```

The idempotency key can also be sent as an `Idempotency-Key` header. It is scoped to a user and show. Reusing it with the same set of seats returns HTTP 200 and the original reservation (including `cancelled` if it was later cancelled); a different set returns 409 `idempotency-conflict`. New successful reservations return 201. A taken seat returns 409 `seat-taken`; exceeding the per-user limit returns 409 `per-user-limit`. Multi-seat requests are **all-or-nothing**. There are no timed holds; `held` is always zero. Money is integer paise.

## Burst and tests

```sh
python3 -m unittest -v test_app.py
python3 burst.py http://localhost:8000 --requests 20000 --concurrency 500
```

The burst creates a fresh show and tests a hot seat, idempotent replay and key conflict, other seats, and a shared user hitting the limit. It prints the outcome distribution, hot-seat winner count, 5xx/transport error count, and final reconciliation; it exits nonzero if the core checks fail. Set `AUTH_SECRET` and `ADMIN_TOKEN` in the burst environment to match the server. A smaller `--requests 1000 --concurrency 100` is useful for a quick run.

Against the public deployment, run this single command from a clean checkout (Python 3.12+; no packages required):

```sh
python3 burst.py https://seat-reservation-at-scale-production.up.railway.app --requests 20000 --concurrency 100
```

The public Railway trial passed this setting. The measured 500-client hosted run had gateway errors, as recorded below; use that setting to probe the capacity limit, not as a clean hosted baseline.

## Deployment

Build the Dockerfile on a platform that supports a persistent disk/volume and route the platform's `PORT` to the service. Set `DATABASE_PATH=/data/reservations.db`, mount durable storage at `/data`, and run exactly one process/instance. Configure a readiness probe at `/health/ready`. Application logs are JSON on stdout; metrics are at `/metrics`. If the host cannot provide durable storage, use a managed SQL database instead of local SQLite before submitting a live URL.

## Live deployment

- Repository: https://github.com/Sak081726/seat-reservation-at-scale
- API: https://seat-reservation-at-scale-production.up.railway.app
- Readiness: https://seat-reservation-at-scale-production.up.railway.app/health/ready
- Metrics: https://seat-reservation-at-scale-production.up.railway.app/metrics
- Railway logs (owner access): https://railway.com/project/d57e2962-5551-4c4c-889f-ab3260308d6a/logs?environmentId=5f37a31f-aeab-4dcc-bbbd-3cea9e33c92d

The Railway service uses one replica and a persistent volume mounted at `/data`, with `/health/ready` configured as its deploy healthcheck. The public demo currently uses the documented development credentials above so reviewers can reproduce the burst without a private handoff. Do not put real users or payments on this demo; replace those credentials and use a proper identity provider before production use. The Railway trial may expire or pause the live URL after its included credit or time is exhausted.

Live burst results on 2026-10-01: 1,000 requests at concurrency 100 and 20,000 requests at concurrency 100 both had one winner for the contested seat, zero 5xx/transport failures, and exact seat reconciliation. At concurrency 500, the 20,000-request run still reconciled with one winner but had 421 HTTP 502 responses and 8 transport failures from the Railway path. The hosted demonstration therefore meets the 20,000-request correctness check at concurrency 100; the 500-client failure is a measured capacity limit, not a claim of clean service at that rate. The same 20,000/500 run completed without 5xx locally.
