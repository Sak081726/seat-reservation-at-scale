import concurrent.futures
import os
import tempfile
import unittest

import app


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.temp.name, "test.db")
        app.initialize()
        self.show = app.create_show({"name": "test", "seats": [f"A{i}" for i in range(1, 21)],
                                     "price_paise": 25000, "per_user_limit": 4})["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_hot_seat_has_one_winner_and_clean_declines(self):
        def attempt(i):
            try:
                result, replay = app.reserve(self.show, f"user-{i}",
                                             {"seats": ["A1"], "idempotency_key": f"key-{i}"})
                return 201, result, replay
            except app.APIError as err:
                return err.status, err.reason, False
        with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
            results = list(pool.map(attempt, range(200)))
        self.assertEqual(sum(row[0] == 201 for row in results), 1)
        self.assertEqual(sum(row[0] == 409 and row[1] == "seat-taken" for row in results), 199)
        state = app.get_show(self.show)["counts"]
        self.assertEqual(state, {"available": 19, "held": 0, "confirmed": 1, "total_seats": 20})

    def test_limit_and_identity_under_parallel_requests(self):
        def attempt(i):
            try:
                return app.reserve(self.show, "same-user", {"seats": [f"A{i}"],
                                                               "idempotency_key": f"key-{i}"})[0]
            except app.APIError as err:
                return err.reason
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(attempt, range(1, 11)))
        self.assertEqual(sum(isinstance(v, dict) for v in results), 4)
        self.assertEqual(results.count("per-user-limit"), 6)

    def test_idempotency_multi_seat_and_cancel(self):
        original, replay = app.reserve(self.show, "alice", {"seats": ["A2", "A1"],
                                                             "idempotency_key": "same"})
        self.assertFalse(replay)
        again, replay = app.reserve(self.show, "alice", {"seats": ["A1", "A2"],
                                                          "idempotency_key": "same"})
        self.assertTrue(replay)
        self.assertEqual(original, again)
        with self.assertRaises(app.APIError) as error:
            app.reserve(self.show, "alice", {"seats": ["A3"], "idempotency_key": "same"})
        self.assertEqual(error.exception.reason, "idempotency-conflict")
        with self.assertRaises(app.APIError) as error:
            app.cancel(original["reservation_id"], "bob")
        self.assertEqual(error.exception.status, 403)
        app.cancel(original["reservation_id"], "alice")
        self.assertEqual(app.get_show(self.show)["counts"]["available"], 20)
        replacement, _ = app.reserve(self.show, "bob", {"seats": ["A1"], "idempotency_key": "new"})
        self.assertEqual(replacement["status"], "confirmed")
        self.assertEqual(app.reserve(self.show, "alice", {"seats": ["A1", "A2"],
                                                          "idempotency_key": "same"})[0]["status"], "cancelled")

    def test_multi_seat_is_all_or_nothing(self):
        app.reserve(self.show, "alice", {"seats": ["A1"], "idempotency_key": "one"})
        with self.assertRaises(app.APIError) as error:
            app.reserve(self.show, "bob", {"seats": ["A1", "A2"], "idempotency_key": "two"})
        self.assertEqual(error.exception.reason, "seat-taken")
        self.assertEqual(app.get_show(self.show)["seats"]["A2"], "available")


if __name__ == "__main__":
    unittest.main()
