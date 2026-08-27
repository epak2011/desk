import unittest

import user_state_store


class FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row


class UserStateStoreTests(unittest.TestCase):
    def test_missing_user_fails_closed(self):
        with self.assertRaises(ValueError):
            user_state_store.load(FakeCursor(), "")

    def test_load_always_scopes_query(self):
        cursor = FakeCursor(({"watchlist": ["AAPL"]},))
        value = user_state_store.load(cursor, "00000000-0000-0000-0000-000000000001")
        self.assertEqual(value["watchlist"], ["AAPL"])
        self.assertEqual(cursor.calls[0][1], ("00000000-0000-0000-0000-000000000001",))

    def test_save_always_scopes_upsert(self):
        cursor = FakeCursor()
        user_state_store.save(cursor, "00000000-0000-0000-0000-000000000001", {"holdings": {}})
        self.assertEqual(cursor.calls[0][1][0], "00000000-0000-0000-0000-000000000001")

    def test_two_users_generate_distinct_storage_keys(self):
        cursor = FakeCursor()
        first = "00000000-0000-0000-0000-000000000001"
        second = "00000000-0000-0000-0000-000000000002"
        user_state_store.save(cursor, first, {"watchlist": ["AAPL"]})
        user_state_store.save(cursor, second, {"watchlist": ["MSFT"]})
        self.assertNotEqual(cursor.calls[0][1][0], cursor.calls[1][1][0])


if __name__ == "__main__":
    unittest.main()
