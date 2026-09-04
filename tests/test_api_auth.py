import io
import json
import os
import unittest
from unittest import mock

import api_auth


class _Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class ApiAuthTests(unittest.TestCase):
    def test_verified_identity_comes_from_supabase_response(self):
        with (
            mock.patch.dict(os.environ, {"SUPABASE_URL": "https://demo.supabase.co", "SUPABASE_ANON_KEY": "anon"}),
            mock.patch.object(api_auth.urllib.request, "urlopen", return_value=_Response({
                "id": "user-123",
                "email": "demo@example.invalid",
                "user_metadata": {"display_name": "Demo"},
            })) as request,
        ):
            identity = api_auth.verify_access_token("jwt-value")
        self.assertEqual(identity.user_id, "user-123")
        self.assertEqual(identity.display_name, "Demo")
        sent = request.call_args.args[0]
        self.assertEqual(sent.headers["Authorization"], "Bearer jwt-value")

    def test_missing_token_is_rejected_before_network(self):
        with self.assertRaises(api_auth.TokenVerificationError):
            api_auth.verify_access_token("")

    def test_unauthorized_supabase_response_is_safe(self):
        error = api_auth.urllib.error.HTTPError("url", 401, "no", {}, io.BytesIO(b"{}"))
        with (
            mock.patch.dict(os.environ, {"SUPABASE_URL": "https://demo.supabase.co", "SUPABASE_ANON_KEY": "anon"}),
            mock.patch.object(api_auth.urllib.request, "urlopen", side_effect=error),
            self.assertRaisesRegex(api_auth.TokenVerificationError, "invalid or expired"),
        ):
            api_auth.verify_access_token("expired")


if __name__ == "__main__":
    unittest.main()
