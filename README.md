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

Set `AUTH_SECRET` to the same value for the server and token generation. `POST /shows` requires `Authorization: Bearer $ADMIN_TOKEN`. Defaults (`development-secret-change-me` and `development-admin-token`) are provided for local evaluation only. A public deployment should set its own values and provide test credentials to reviewers separately. This token mechanism demonstrates token-derived identity; production would use a trusted identity provider and key rotation.

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

## Deployment

Build the Dockerfile on a platform that supports a persistent disk/volume and route the platform's `PORT` to the service. Set `DATABASE_PATH=/data/reservations.db`, mount durable storage at `/data`, and run exactly one process/instance. Configure a readiness probe at `/health/ready`. Application logs are JSON on stdout; metrics are at `/metrics`. If the host cannot provide durable storage, use a managed SQL database instead of local SQLite before submitting a live URL.

The public repository URL, live service URL, and platform log access should be added here after deployment.
