"""Dependency-free seat reservation HTTP service.

The SQLite writer lock is the serialization point for every booking decision.
Run one application process against one persistent SQLite database file.
"""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from urllib.parse import quote, urlsplit


DB_PATH = os.environ.get("DATABASE_PATH", "data/reservations.db")
AUTH_SECRET = os.environ.get("AUTH_SECRET", "development-secret-change-me")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "development-admin-token")
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_BODY = 1024 * 1024
SEAT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class APIError(Exception):
    def __init__(self, status, reason, message):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message


def connect(create=False):
    path = os.path.abspath(DB_PATH)
    database = path if create else "file:" + quote(path) + "?mode=rw"
    con = sqlite3.connect(database, timeout=30, isolation_level=None, uri=not create)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def initialize():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    con = connect(create=True)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS shows (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price_paise INTEGER NOT NULL CHECK(price_paise >= 0),
            per_user_limit INTEGER NOT NULL CHECK(per_user_limit > 0)
        );
        CREATE TABLE IF NOT EXISTS reservations (
            id TEXT PRIMARY KEY,
            show_id TEXT NOT NULL REFERENCES shows(id),
            user_id TEXT NOT NULL,
            seats_json TEXT NOT NULL,
            amount_paise INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('confirmed','cancelled')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS seats (
            show_id TEXT NOT NULL REFERENCES shows(id),
            seat_id TEXT NOT NULL,
            reservation_id TEXT REFERENCES reservations(id),
            PRIMARY KEY(show_id, seat_id)
        );
        CREATE INDEX IF NOT EXISTS seats_reservation_idx ON seats(reservation_id);
        CREATE INDEX IF NOT EXISTS reservations_user_idx ON reservations(show_id,user_id,status);
        CREATE TABLE IF NOT EXISTS idempotency (
            show_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            seats_json TEXT NOT NULL,
            reservation_id TEXT NOT NULL REFERENCES reservations(id),
            PRIMARY KEY(show_id,user_id,key)
        );
        CREATE TABLE IF NOT EXISTS metrics (
            name TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            value INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(name,reason)
        );
        """)
    finally:
        con.close()


def metric(con, name, reason=""):
    con.execute("""INSERT INTO metrics(name,reason,value) VALUES(?,?,1)
        ON CONFLICT(name,reason) DO UPDATE SET value=value+1""", (name, reason))


def decline(con, reason, message):
    metric(con, "reservations_declined_total", reason)
    con.execute("COMMIT")
    raise APIError(409, reason, message)


def create_show(body):
    name = body.get("name")
    seats = body.get("seats")
    price = body.get("price_paise")
    limit = body.get("per_user_limit", 4)
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise APIError(400, "invalid-request", "name must be a nonempty string of at most 120 characters")
    if not isinstance(seats, list) or not 1 <= len(seats) <= 10000 or any(
        not isinstance(s, str) or not SEAT_RE.fullmatch(s) for s in seats
    ) or len(set(seats)) != len(seats):
        raise APIError(400, "invalid-request", "seats must be 1-10000 distinct seat identifiers")
    if type(price) is not int or price < 0 or price > 1_000_000_000:
        raise APIError(400, "invalid-request", "price_paise must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise APIError(400, "invalid-request", "per_user_limit must be a positive integer")
    show_id = str(uuid.uuid4())
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO shows VALUES(?,?,?,?)", (show_id, name, price, limit))
        con.executemany("INSERT INTO seats(show_id,seat_id) VALUES(?,?)", ((show_id, s) for s in seats))
        con.execute("COMMIT")
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return {"id": show_id, "name": name, "price_paise": price,
            "per_user_limit": limit, "seats": {s: "available" for s in seats},
            "counts": {"available": len(seats), "held": 0, "confirmed": 0, "total_seats": len(seats)}}


def reserve(show_id, user_id, body, header_key=None):
    seats = body.get("seats")
    key = header_key or body.get("idempotency_key")
    if not isinstance(seats, list) or not 1 <= len(seats) <= 100 or any(
        not isinstance(s, str) or not SEAT_RE.fullmatch(s) for s in seats
    ) or len(set(seats)) != len(seats):
        raise APIError(400, "invalid-request", "seats must be 1-100 distinct seat identifiers")
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise APIError(400, "invalid-request", "idempotency_key is required (1-200 characters)")
    normalized = sorted(seats)
    seats_json = json.dumps(normalized, separators=(",", ":"))
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute("""SELECT i.seats_json,r.id,r.amount_paise,r.status
            FROM idempotency i JOIN reservations r ON r.id=i.reservation_id
            WHERE i.show_id=? AND i.user_id=? AND i.key=?""", (show_id,user_id,key)).fetchone()
        if existing:
            if existing["seats_json"] != seats_json:
                decline(con, "idempotency-conflict", "key was previously used for different seats")
            metric(con, "reservations_replayed_total")
            con.execute("COMMIT")
            return {"reservation_id": existing["id"], "show_id": show_id, "user_id": user_id,
                    "seats": normalized, "amount_paise": existing["amount_paise"],
                    "status": existing["status"]}, True
        show = con.execute("SELECT price_paise,per_user_limit FROM shows WHERE id=?", (show_id,)).fetchone()
        if not show:
            con.execute("ROLLBACK")
            raise APIError(404, "show-not-found", "show does not exist")
        known = con.execute("SELECT seat_id,reservation_id FROM seats WHERE show_id=? AND seat_id IN (%s)" %
                            ",".join("?" for _ in normalized), (show_id,*normalized)).fetchall()
        if len(known) != len(normalized):
            con.execute("ROLLBACK")
            raise APIError(400, "invalid-seat", "one or more seats do not exist")
        if any(row["reservation_id"] is not None for row in known):
            decline(con, "seat-taken", "one or more seats are already taken")
        owned = con.execute("""SELECT COUNT(*) FROM seats s JOIN reservations r ON r.id=s.reservation_id
            WHERE s.show_id=? AND r.user_id=? AND r.status='confirmed'""", (show_id,user_id)).fetchone()[0]
        if owned + len(normalized) > show["per_user_limit"]:
            decline(con, "per-user-limit", "reservation would exceed the per-user seat limit")
        reservation_id = str(uuid.uuid4())
        amount = show["price_paise"] * len(normalized)
        con.execute("""INSERT INTO reservations(id,show_id,user_id,seats_json,amount_paise,status)
            VALUES(?,?,?,?,?,'confirmed')""", (reservation_id,show_id,user_id,seats_json,amount))
        for seat in normalized:
            changed = con.execute("""UPDATE seats SET reservation_id=?
                WHERE show_id=? AND seat_id=? AND reservation_id IS NULL""",
                (reservation_id,show_id,seat)).rowcount
            if changed != 1:
                raise RuntimeError("seat assignment violated transaction invariant")
        con.execute("INSERT INTO idempotency VALUES(?,?,?,?,?)", (show_id,user_id,key,seats_json,reservation_id))
        metric(con, "reservations_confirmed_total")
        con.execute("COMMIT")
        return {"reservation_id": reservation_id, "show_id": show_id, "user_id": user_id,
                "seats": normalized, "amount_paise": amount, "status": "confirmed"}, False
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def cancel(reservation_id, user_id):
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT user_id,status FROM reservations WHERE id=?", (reservation_id,)).fetchone()
        if not row:
            con.execute("ROLLBACK")
            raise APIError(404, "reservation-not-found", "reservation does not exist")
        if row["user_id"] != user_id:
            con.execute("ROLLBACK")
            raise APIError(403, "forbidden", "only the owner may cancel")
        if row["status"] == "confirmed":
            con.execute("UPDATE seats SET reservation_id=NULL WHERE reservation_id=?", (reservation_id,))
            con.execute("UPDATE reservations SET status='cancelled' WHERE id=?", (reservation_id,))
            metric(con, "reservations_cancelled_total")
        con.execute("COMMIT")
        return {"reservation_id": reservation_id, "status": "cancelled"}
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def get_show(show_id):
    con = connect()
    try:
        con.execute("BEGIN")
        show = con.execute("SELECT * FROM shows WHERE id=?", (show_id,)).fetchone()
        if not show:
            raise APIError(404, "show-not-found", "show does not exist")
        rows = con.execute("SELECT seat_id,reservation_id FROM seats WHERE show_id=? ORDER BY seat_id", (show_id,)).fetchall()
        seat_map = {r["seat_id"]: "confirmed" if r["reservation_id"] else "available" for r in rows}
        confirmed = sum(v == "confirmed" for v in seat_map.values())
        return {"id": show["id"], "name": show["name"], "price_paise": show["price_paise"],
                "per_user_limit": show["per_user_limit"], "seats": seat_map,
                "counts": {"available": len(rows)-confirmed, "held": 0,
                           "confirmed": confirmed, "total_seats": len(rows)}}
    finally:
        con.close()


def get_metrics():
    con = connect()
    try:
        lines = ["# TYPE reservations_confirmed_total counter",
                 "# TYPE reservations_declined_total counter",
                 "# TYPE reservations_replayed_total counter",
                 "# TYPE reservations_cancelled_total counter",
                 "# TYPE seats_available gauge"]
        for row in con.execute("SELECT name,reason,value FROM metrics ORDER BY name,reason"):
            label = "{reason=%s}" % json.dumps(row["reason"]) if row["reason"] else ""
            lines.append(f'{row["name"]}{label} {row["value"]}')
        for row in con.execute("""SELECT show_id,SUM(CASE WHEN reservation_id IS NULL THEN 1 ELSE 0 END) n
            FROM seats GROUP BY show_id"""):
            lines.append(f'seats_available{{show_id={json.dumps(row["show_id"])}}} {row["n"]}')
        return "\n".join(lines) + "\n"
    finally:
        con.close()


def verify_token(token):
    try:
        encoded, signature = token.split(".", 1)
        user = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        expected = hmac.new(AUTH_SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not 1 <= len(user) <= 120 or not hmac.compare_digest(expected, signature):
            return None
        return user
    except (ValueError, UnicodeError):
        return None


def issue_token(user):
    encoded = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=")
    signature = hmac.new(AUTH_SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def log_event(request_id, method, path, status, started, reason=""):
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "request_id": request_id, "method": method, "path": path,
                      "status": status, "reason": reason,
                      "duration_ms": round((time.monotonic()-started)*1000, 2)}), flush=True)


async def handle(reader, writer, executor):
    started = time.monotonic()
    request_id = str(uuid.uuid4())
    method, path, status, reason = "?", "?", 500, ""
    try:
        raw_headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        if len(raw_headers) > 16384:
            raise APIError(431, "headers-too-large", "request headers too large")
        lines = raw_headers.decode("latin1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise APIError(400, "invalid-request", "invalid HTTP request line")
        method, path = parts[0], urlsplit(parts[1]).path
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line:
                raise APIError(400, "invalid-request", "invalid HTTP header")
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
        request_id = headers.get("x-request-id", request_id)[:100]
        if method == "POST":
            try:
                length = int(headers.get("content-length", "-1"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                raise APIError(413, "invalid-request", "Content-Length required and body must be at most 1 MiB")
            raw_body = await asyncio.wait_for(reader.readexactly(length), timeout=30)
            try:
                body = json.loads(raw_body)
            except (ValueError, UnicodeError):
                raise APIError(400, "invalid-json", "request body must be JSON")
            if not isinstance(body, dict):
                raise APIError(400, "invalid-request", "request body must be a JSON object")
        else:
            body = {}
        loop = asyncio.get_running_loop()
        def run(fn, *args):
            return loop.run_in_executor(executor, fn, *args)
        content_type = "application/json"
        if method == "GET" and path == "/health/live":
            status, payload = 200, {"status": "alive"}
        elif method == "GET" and path == "/health/ready":
            try:
                await run(check_ready)
                status, payload = 200, {"status": "ready"}
            except sqlite3.Error:
                status, payload = 503, {"status": "not-ready"}
        elif method == "GET" and path == "/metrics":
            status, payload, content_type = 200, await run(get_metrics), "text/plain; version=0.0.4"
        elif method == "POST" and path == "/shows":
            if not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + ADMIN_TOKEN):
                raise APIError(401, "unauthorized", "admin bearer token required")
            status, payload = 201, await run(create_show, body)
        elif method == "GET" and re.fullmatch(r"/shows/[^/]+", path):
            status, payload = 200, await run(get_show, path.split("/")[2])
        elif method == "POST" and re.fullmatch(r"/shows/[^/]+/reserve", path):
            user = authenticated_user(headers)
            payload, replay = await run(reserve, path.split("/")[2], user, body, headers.get("idempotency-key"))
            status = 200 if replay else 201
            if replay:
                reason = "idempotent-replay"
        elif method == "POST" and re.fullmatch(r"/reservations/[^/]+/cancel", path):
            user = authenticated_user(headers)
            status, payload = 200, await run(cancel, path.split("/")[2], user)
        else:
            raise APIError(404, "not-found", "endpoint does not exist")
    except APIError as err:
        status, reason = err.status, err.reason
        payload = {"error": reason, "message": err.message}
        content_type = "application/json"
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        status, reason, payload, content_type = 400, "invalid-request", {"error": "invalid-request"}, "application/json"
    except Exception as err:
        status, reason, payload, content_type = 500, "internal-error", {"error": "internal-error"}, "application/json"
        print(json.dumps({"request_id": request_id, "exception": repr(err)}), file=sys.stderr, flush=True)
    response = payload.encode() if isinstance(payload, str) else json.dumps(payload, separators=(",", ":")).encode()
    reason_phrase = {200:"OK",201:"Created",400:"Bad Request",401:"Unauthorized",403:"Forbidden",
                     404:"Not Found",409:"Conflict",413:"Content Too Large",431:"Request Header Fields Too Large",
                     500:"Internal Server Error",503:"Service Unavailable"}.get(status,"Error")
    writer.write((f"HTTP/1.1 {status} {reason_phrase}\r\nContent-Type: {content_type}\r\n"
                  f"Content-Length: {len(response)}\r\nConnection: close\r\nX-Request-ID: {request_id}\r\n\r\n").encode() + response)
    try:
        await writer.drain()
    except ConnectionError:
        pass
    writer.close()
    log_event(request_id, method, path, status, started, reason)


def authenticated_user(headers):
    auth = headers.get("authorization", "")
    user = verify_token(auth[7:]) if auth.startswith("Bearer ") else None
    if user is None:
        raise APIError(401, "unauthorized", "valid user bearer token required")
    return user


def check_ready():
    con = connect()
    try:
        con.execute("SELECT COUNT(*) FROM shows").fetchone()
    finally:
        con.close()


async def serve():
    initialize()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="database")
    server = await asyncio.start_server(lambda r,w: handle(r,w,executor), HOST, PORT, backlog=4096)
    print(json.dumps({"event": "started", "port": PORT, "database": DB_PATH}), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "token":
        print(issue_token(sys.argv[2]))
    else:
        asyncio.run(serve())
