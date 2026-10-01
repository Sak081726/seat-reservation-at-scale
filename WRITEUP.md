# Design notes

## Atomic booking decision

Every reserve and cancel executes inside `BEGIN IMMEDIATE` on one SQLite database. SQLite permits only one writer at a time, so the transaction checks the idempotency key, seat ownership, and the user's current seat count against a serialized state. It then inserts the reservation and conditionally assigns each seat with `UPDATE ... WHERE reservation_id IS NULL`. The primary key `(show_id, seat_id)` represents each seat exactly once. All assignments commit together; an error rolls everything back. This is all-or-nothing for multi-seat requests. A single SQLite writer also avoids row-lock ordering deadlocks. The tradeoff is write throughput; this design must run as one process against one persistent database file and does not horizontally scale.

The authoritative seat state is the `seats` table. `GET /shows/{id}` reads the show and seats in one read transaction, and derives counts from those same rows. Thus available + held + confirmed always equals total seats. There are no temporary holds in this version, so held is zero.

## Idempotency and payment

The `idempotency` table has a primary key on `(show_id, user_id, key)` and stores a canonical sorted seat list and reservation ID. The key lookup and reservation commit occur in one writer transaction. A retry with the same set returns the original reservation and does not change any seat or amount. A different set gets 409. Keys remain after cancellation, so an old retry cannot book a released seat again. No payment gateway is integrated; `amount_paise` is a booking amount, not a real charge. A real payment integration would need a durable charge ledger or payment-provider idempotency key coupled to the reservation state.

## Cancellation and identity

Reservations are confirmed immediately. Only a token-authenticated owner can cancel. Cancellation clears the seat assignments and marks the reservation cancelled in one transaction. A second cancellation is harmless. Released seats can then be booked by others. The API derives `user_id` from a signed bearer token and ignores any `user_id` in the JSON body. The HMAC token issuer is intentionally small for this exercise; production would verify tokens from a real identity provider.

## Consistency and availability

This is a single-node service and database. If database storage becomes inaccessible, readiness fails and bookings cannot proceed. The design favors consistency over accepting reservations while the system of record is unavailable. A deployment must mount persistent storage; a platform's ephemeral container filesystem would lose bookings on restart. For horizontal scale, move the same transactional rules to PostgreSQL with deterministic seat row locking, a unique idempotency constraint, and a transaction-scoped per-user guard.

## Observability

`/metrics` exposes confirmed, declined (by reason), replayed, and cancelled counters and a per-show available-seat gauge computed from the database. A replay is counted separately because it returns the original success, rather than a decline. JSON logs contain a request ID, method, path, status, domain reason, and latency. At 2am I would page for any 5xx increase, readiness failures, or a reconciliation mismatch. I would also investigate sustained 409 spikes, latency growth, or a burst with no confirmations. Counters are stored in the database so restarts do not reset them.

## AI usage and next steps

AI assisted with implementation, test design, and documentation. The design choices to review closely are the SQLite single-writer boundary, idempotency scope, cancellation semantics, and deployment storage. The owner should run the burst against the deployed URL, inspect metrics and logs, and be prepared to explain those choices live. Next I would add a PostgreSQL implementation for multi-instance scaling, provider-backed authentication, a payment workflow, and load tests across cold starts and process restarts.
