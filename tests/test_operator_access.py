import unittest

from operator_access import owner_access_allowed


class OwnerAccessTests(unittest.TestCase):
    def test_exact_owner_identity_is_allowed_case_insensitively(self):
        identity = {"user_id": "user-123", "email": "EPAK2011@gmail.com"}
        self.assertTrue(owner_access_allowed(identity, "epak2011@gmail.com"))

    def test_other_authenticated_user_is_denied(self):
        identity = {"user_id": "user-456", "email": "someone@example.com"}
        self.assertFalse(owner_access_allowed(identity, "epak2011@gmail.com"))

    def test_missing_user_or_owner_configuration_is_denied(self):
        self.assertFalse(owner_access_allowed({"email": "epak2011@gmail.com"}, "epak2011@gmail.com"))
        self.assertFalse(owner_access_allowed({"user_id": "user-123", "email": "epak2011@gmail.com"}, ""))


if __name__ == "__main__":
    unittest.main()
