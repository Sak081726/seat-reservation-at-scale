# Design notes

## Atomic booking decision

Every reserve and cancel executes inside `BEGIN IMMEDIATE` on one SQLite database. SQLite permits only one writer at a time, so the transaction checks the idempotency key, seat ownership, and the user's current seat count against a serialized state. It then inserts the reservation and conditionally assigns each seat with `UPDATE ... WHERE reservation_id IS NULL`. The primary key `(show_id, seat_id)` represents each seat exactly once. All assignments commit together; an error rolls everything back. This is all-or-nothing for multi-seat requests. A single SQLite writer also avoids row-lock ordering deadlocks. The tradeoff is write throughput; this design must run as one process against one persistent database file and does not horizontally scale.

The authoritative seat state is the `seats` table. `GET /shows/{id}` reads the show and seats in one read transaction, and derives counts from those same rows. Thus available + held + confirmed always equals total seats. There are no temporary holds in this version, so held is zero.

## Idempotency and payment

The `idempotency` table has a primary key on `(show_id, user_id, key)` and stores a canonical sorted seat list and reservation ID. The key lookup and reservation commit occur in one writer transaction. A retry with the same set returns the original reservation and does not change any seat or amount. A different set gets 409. Keys remain after cancellation, so an old retry cannot book a released seat again. No payment gateway is integrated; `amount_paise` is a booking amount, not a real charge. A real payment integration would need a durable charge ledger or payment-provider idempotency key coupled to the reservation state.

## Cancellation and identity

Reservations are confirmed immediately. This service does not create temporary holds, so there is no hold expiry clock or sweeper. The chosen release model is explicit cancellation: only a token-authenticated owner can cancel. Cancellation clears the seat assignments and marks the reservation cancelled in one transaction. A second cancellation is harmless. Released seats can then be booked by others. The API derives `user_id` from a signed bearer token and ignores any `user_id` in the JSON body. The HMAC token issuer is intentionally small for this exercise; production would verify tokens from a real identity provider.

## Consistency and availability

This is a single-node service and database. If database storage becomes inaccessible, readiness fails and bookings cannot proceed. The design favors consistency over accepting reservations while the system of record is unavailable. A deployment must mount persistent storage; a platform's ephemeral container filesystem would lose bookings on restart. For horizontal scale, move the same transactional rules to PostgreSQL with deterministic seat row locking, a unique idempotency constraint, and a transaction-scoped per-user guard.

## Observability

`/metrics` exposes confirmed, declined (by reason), replayed, and cancelled counters and a per-show available-seat gauge computed from the database. A replay is counted separately because it returns the original success, rather than a decline. JSON logs contain a request ID, method, path, status, domain reason, and latency. At 2am I would page for any 5xx increase, readiness failures, or a reconciliation mismatch. I would also investigate sustained 409 spikes, latency growth, or a burst with no confirmations. Counters are stored in the database so restarts do not reset them.

## Deployed load observations

The Railway trial deployment uses one service replica with a persistent `/data` volume and `/health/ready` as its deploy healthcheck. On 2026-10-01, the live 20,000-request run at concurrency 100 returned 103 new confirmations plus the seeded reservation, 1,000 idempotent replays, 1,000 idempotency conflicts, 1,200 per-user-limit declines, and 16,697 taken-seat declines. Exactly one request won the contested seat, no request returned a 5xx or transport error, and the final state was 104 confirmed, 6 available, 0 held, 110 total. A 1,000-request live run at concurrency 100 also passed with zero 5xx errors.

At concurrency 500, 20,000 live requests still ended with one contested-seat winner and exact reconciliation, but 421 requests returned HTTP 502 and 8 had transport errors. The application does not emit 502, so this is consistent with overload in the hosted request path; the specific source of the gateway failures was not proven. The same run completed without 5xx on a local process. This single-writer design should not be presented as a horizontally scalable production system. To raise hosted throughput, I would move the transaction to PostgreSQL, measure the gateway and storage latency separately, and repeat the burst with connection reuse and platform resource monitoring.

## AI usage and next steps

The assignment and direction to build and deploy the service came from me. I used Codex extensively: it proposed the Python standard-library and SQLite design, wrote the application, tests, burst runner, Docker configuration, README, and this write-up, and operated the GitHub and Railway deployment. The concrete implementation choices—`BEGIN IMMEDIATE`, a single persistent database volume, immediate confirmation with owner cancellation, HMAC exercise tokens, and the burst mix—were made by the AI assistant during that work. Codex ran the unit tests, live bursts, and deployment checks and inspected metrics and logs; I still need to review the implementation and repeat key checks myself before an interview. The hosted 500-client failure is an observed limit, not a passing result.

Next I would add a PostgreSQL implementation for multi-instance scaling, provider-backed authentication, a payment workflow, and load tests across cold starts and process restarts. I would also alert on gateway 502s and measure saturation before claiming the hosted service meets a higher concurrency target.
