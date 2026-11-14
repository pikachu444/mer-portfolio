"""Offline OAuth/JWT tests: no user login, API quota, or delivery is used."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from chatgpt_auth import (
    ChatGPTAuth, ChatGPTAuthError, CredentialStore, DISCOVERY_URL,
    DYNAMIC_CLIENT_ID, ISSUER, RESOURCE, SCOPES, TOKEN_URL,
)


PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
JWK.update(kid="test-key", use="sig", alg="RS256")


def token(*, client_id="oaiapp_test", subject="subject-one", nonce="nonce", **changes):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": client_id, "sub": subject, "email": "user@example.com",
              "iat": now, "exp": now + 3600, "nonce": nonce}
    claims.update(changes)
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"})


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload, self.status_code, self.closed = payload, status, False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.calls = []
        self.tokens = {}
        self.revocation_status = 200

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if url == DISCOVERY_URL:
            return FakeResponse({"issuer": ISSUER, "jwks_uri": ISSUER + "/jwks",
                                 "revocation_endpoint": ISSUER + "/revoke"})
        return FakeResponse({"keys": [JWK]})

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if url.endswith("/revoke"):
            return FakeResponse(status=self.revocation_status)
        return FakeResponse(dict(self.tokens))


class ChatGPTAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = CredentialStore(Path(self.temporary.name) / "oauth")
        self.session = FakeSession()
        self.auth = ChatGPTAuth(self.store, session=self.session)

    def register(self, *, subject="subject-one", client_id="oaiapp_test"):
        attempt = self.auth.prepare_login("http://127.0.0.1:1455/auth/callback", new_account=True)
        self.session.tokens = {"access_token": "fake-access", "refresh_token": "fake-refresh", "token_type": "Bearer",
                               "id_token": token(client_id=client_id, subject=subject, nonce=attempt["nonce"]),
                               "scope": SCOPES, "expires_in": 3600}
        account = self.auth.complete_login(attempt, {"state": attempt["state"], "code": "fake-code", "client_id": client_id})
        return account, attempt

    def test_pkce_dynamic_registration_and_owner_only_storage(self):
        account, attempt = self.register()
        parameters = parse_qs(urlparse(attempt["authorization_url"]).query)
        self.assertEqual(parameters["client_id"], [DYNAMIC_CLIENT_ID])
        self.assertEqual(parameters["agent_name_hint"], ["Mer Portfolio"])
        self.assertEqual(parameters["code_challenge_method"], ["S256"])
        self.assertEqual(parameters["ext_agent_host_id"], [self.store.host_id()])
        profile = self.store.read_profile(account)
        self.assertEqual(profile["subject"], "subject-one")
        self.assertEqual(profile["client_id"], "oaiapp_test")
        exchange = next(kwargs["data"] for method, url, kwargs in self.session.calls if method == "post" and url == TOKEN_URL)
        self.assertEqual(exchange["client_id"], "oaiapp_test")
        self.assertEqual(exchange["code_verifier"], attempt["verifier"])
        self.assertEqual(exchange["redirect_uri"], attempt["redirect_uri"])
        self.assertEqual(exchange["resource"], RESOURCE)
        if os.name != "nt":
            self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.store.profiles_dir / (account + ".json")).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fake-access", json.dumps(self.store.status()))
        self.assertNotIn("fake-refresh", json.dumps(self.store.status()))

    def test_state_denial_and_missing_client_never_exchange(self):
        for callback in ({"state": "wrong", "code": "fake"}, {"state": "한글", "code": "fake"},
                         {"error": "access_denied"}, {"code": "fake"}):
            attempt = self.auth.prepare_login("http://127.0.0.1:1455/auth/callback", new_account=True)
            callback = dict(callback)
            callback.setdefault("state", attempt["state"])
            with self.assertRaises(ChatGPTAuthError):
                self.auth.complete_login(attempt, callback)
        self.assertFalse(any(method == "post" for method, _, _ in self.session.calls))

    def test_expired_authorization_code_retains_issued_registration_without_persisting_it(self):
        attempt = self.auth.prepare_login("http://127.0.0.1:1455/auth/callback", new_account=True)
        with patch.object(self.session, "post", return_value=FakeResponse({"error": "invalid_grant"}, status=400)):
            with self.assertRaises(ChatGPTAuthError) as failed:
                self.auth.complete_login(attempt, {"state": attempt["state"], "code": "expired", "client_id": "oaiapp_test"})
        self.assertEqual(failed.exception.oauth_error, "invalid_grant")
        self.assertEqual(self.store.status()["accounts"], [])
        fresh = self.auth.prepare_login(attempt["redirect_uri"], registration_client_id=attempt["issued_client_id"])
        params = parse_qs(urlparse(fresh["authorization_url"]).query)
        self.assertEqual(params["client_id"], ["oaiapp_test"])
        self.assertNotIn("agent_name_hint", params)
        self.assertNotEqual(fresh["state"], attempt["state"])
        self.assertNotEqual(fresh["nonce"], attempt["nonce"])
        self.assertNotEqual(fresh["verifier"], attempt["verifier"])

    def test_signature_issuer_audience_expiration_and_nonce_are_checked(self):
        for changes in ({"iss": "https://attacker.example"}, {"aud": "other-client"}, {"exp": int(time.time()) - 5},
                        {"nonce": "different"}, {"sub": ""}):
            with self.subTest(changes=changes), self.assertRaises(ChatGPTAuthError):
                self.auth.validate_id_token(token(**changes), "oaiapp_test", nonce="nonce")
        fake_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {"iss": ISSUER, "aud": "oaiapp_test", "sub": "subject", "iat": int(time.time()), "exp": int(time.time()) + 60}
        bad_token = jwt.encode(claims, fake_key, algorithm="RS256", headers={"kid": "test-key"})
        with self.assertRaises(ChatGPTAuthError):
            self.auth.validate_id_token(bad_token, "oaiapp_test")

    def test_no_plan_scope_and_returning_identity_mismatch_preserve_account(self):
        account, _ = self.register()
        before = self.store.read_profile(account)
        attempt = self.auth.prepare_login("http://127.0.0.1:54321/auth/callback")
        parameters = parse_qs(urlparse(attempt["authorization_url"]).query)
        self.assertEqual(parameters["client_id"], ["oaiapp_test"])
        self.assertNotIn("agent_name_hint", parameters)
        self.assertIn("id_token_hint", parameters)
        self.session.tokens["id_token"] = token(subject="wrong-account", nonce=attempt["nonce"])
        with self.assertRaises(ChatGPTAuthError):
            self.auth.complete_login(attempt, {"state": attempt["state"], "code": "fake-code"})
        self.assertEqual(self.store.read_profile(account), before)
        self.session.tokens["id_token"] = token(nonce=attempt["nonce"])
        self.session.tokens["scope"] = "openid email"
        with self.assertRaises(ChatGPTAuthError):
            self.auth.complete_login(attempt, {"state": attempt["state"], "code": "fake-code"})
        self.assertEqual(self.store.read_profile(account), before)

    def test_accounts_with_same_email_remain_separate(self):
        first, _ = self.register(client_id="oaiapp_first")
        second, _ = self.register(client_id="oaiapp_second")
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.store.status()["accounts"]), 2)
        self.store.select(first)
        self.assertEqual(self.store.selected_account(), first)
        self.assertEqual(self.store.read_profile(second)["client_id"], "oaiapp_second")

    def test_running_auth_remains_pinned_when_active_account_changes(self):
        first, _ = self.register(client_id="oaiapp_first")
        first_auth = ChatGPTAuth(self.store, session=self.session)
        first_auth.access_token()
        self.register(client_id="oaiapp_second")
        first_auth.access_token()
        self.assertEqual(first_auth.account, first)

    def test_rotated_refresh_serialized_across_callers(self):
        account, _ = self.register()
        record = self.store.read_profile(account)
        record["expires_at"] = 0
        with self.store.locked():
            self.store.save_profile(record)
        self.session.calls.clear()
        self.session.tokens = {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 3600}
        other_auth = ChatGPTAuth(CredentialStore(self.store.directory), session=self.session)
        with ThreadPoolExecutor(2) as executor:
            values = list(executor.map(lambda auth: auth.access_token(), [self.auth, other_auth]))
        self.assertEqual(values, ["rotated-access", "rotated-access"])
        refreshes = [kwargs["data"] for method, url, kwargs in self.session.calls if method == "post" and url == TOKEN_URL]
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(refreshes[0]["client_id"], "oaiapp_test")
        self.assertNotIn("scope", refreshes[0])
        self.assertEqual(self.store.read_profile(account)["refresh_token"], "rotated-refresh")

    def test_logout_preserves_registration_host_and_clears_tokens(self):
        account, _ = self.register()
        host = self.store.host_id()
        self.session.revocation_status = 503
        self.assertFalse(self.auth.logout())
        record = self.store.read_profile(account)
        self.assertEqual(record["client_id"], "oaiapp_test")
        self.assertEqual(self.store.host_id(), host)
        self.assertNotIn("access_token", record)
        self.assertNotIn("refresh_token", record)
        self.assertNotIn("id_token", record)
        attempt = self.auth.prepare_login("http://127.0.0.1:1455/auth/callback")
        self.assertNotIn("id_token_hint", parse_qs(urlparse(attempt["authorization_url"]).query))
        with self.assertRaises(ChatGPTAuthError):
            self.auth.access_token()

    def test_loopback_login_runs_with_fake_browser_and_fake_openai(self):
        threads = []

        def fake_browser(url):
            query = parse_qs(urlparse(url).query)
            self.session.tokens = {"access_token": "fake-access", "refresh_token": "fake-refresh", "expires_in": 3600,
                                   "scope": SCOPES, "id_token": token(nonce=query["nonce"][0])}
            callback_url = query["redirect_uri"][0] + "?state=" + query["state"][0] + "&code=fake-code&client_id=oaiapp_test"

            def callback():
                with urlopen(callback_url, timeout=3) as response:
                    self.assertEqual(response.status, 200)

            thread = threading.Thread(target=callback)
            threads.append(thread)
            thread.start()
            return True

        with patch("chatgpt_auth.webbrowser.open", side_effect=fake_browser), patch("builtins.print"):
            account = self.auth.login(new_account=True, timeout=3)
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(self.store.read_profile(account)["subject"], "subject-one")

    def test_storage_cannot_be_inside_repository(self):
        with self.assertRaises(ChatGPTAuthError):
            CredentialStore(Path(__file__).resolve().parents[1] / "output" / "chatgpt-auth")


if __name__ == "__main__":
    unittest.main()
