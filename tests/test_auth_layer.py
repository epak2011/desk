import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import auth_layer


class AuthLayerTests(unittest.TestCase):
    def test_google_oauth_flow_uses_pkce_and_scoped_callback(self):
        flow = auth_layer.new_google_oauth_flow("https://x.supabase.co", "https://desk.example")
        query = parse_qs(urlparse(flow["authorize_url"]).query)
        self.assertEqual(query["provider"], ["google"])
        self.assertEqual(query["code_challenge_method"], ["s256"])
        self.assertIn("oauth_state=" + flow["state"], flow["redirect_uri"])
        self.assertNotIn(flow["verifier"], flow["authorize_url"])

    def test_configuration_requires_both_public_values(self):
        self.assertTrue(auth_layer.configured("https://x.supabase.co", "anon"))
        self.assertFalse(auth_layer.configured("", "anon"))

    @patch("auth_layer._request")
    def test_sign_in_returns_scoped_identity(self, request):
        request.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "user": {"id": "123", "email": "a@example.com", "user_metadata": {"display_name": "Elle"}},
        }
        session = auth_layer.sign_in("https://x.supabase.co", "anon", "a@example.com", "secret")
        self.assertEqual(session.user_id, "123")
        self.assertEqual(
            auth_layer.public_identity(session),
            {"user_id": "123", "email": "a@example.com", "display_name": "Elle"},
        )

    @patch("auth_layer._request")
    def test_signup_sends_display_name_as_private_auth_metadata(self, request):
        request.return_value = {"user": {"id": "123"}}
        auth_layer.sign_up("https://x.supabase.co", "anon", "a@example.com", "secret", "Elle")
        self.assertEqual(request.call_args.args[2]["data"], {"display_name": "Elle"})

    @patch("auth_layer._request")
    def test_signup_records_terms_acceptance(self, request):
        request.return_value = {"user": {"id": "123"}}
        auth_layer.sign_up(
            "https://x.supabase.co", "anon", "a@example.com", "secret", "Elle",
            terms_accepted=True,
        )
        metadata = request.call_args.args[2]["data"]
        self.assertTrue(metadata["terms_accepted"])
        self.assertEqual(metadata["terms_version"], auth_layer.TERMS_VERSION)
        self.assertTrue(metadata["terms_accepted_at"].endswith("+00:00"))

    @patch("auth_layer._request")
    def test_unconfirmed_signup_does_not_create_session(self, request):
        request.return_value = {"user": {"id": "123"}}
        self.assertIsNone(auth_layer.sign_up("https://x.supabase.co", "anon", "a@example.com", "secret"))

    @patch("auth_layer._request")
    def test_pkce_exchange_returns_session(self, request):
        request.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
            "user": {"id": "123", "email": "a@example.com"},
        }
        session = auth_layer.exchange_pkce_code("https://x.supabase.co", "anon", "code", "verifier")
        self.assertEqual(session.user_id, "123")
        self.assertEqual(request.call_args.args[2], {"auth_code": "code", "code_verifier": "verifier"})
