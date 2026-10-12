"""Offline model-catalog and SSE tests; no real inference requests."""

import json
import unittest
from unittest.mock import Mock

import requests

from chatgpt_auth import ChatGPTAuthError, RESOURCE
from chatgpt_client import ChatGPTClient, ChatGPTClientError, _events


def completed(text='{"portfolio_decisions": []}'):
    return {"type": "response.completed", "response": {
        "status": "completed", "model": "gpt-6.1-sol-snapshot", "usage": {"input_tokens": 100, "output_tokens": 20},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }}


class FakeResponse:
    def __init__(self, payload=None, events=None, status=200, interrupted=False):
        self.payload, self.events, self.status_code = payload, events or [], status
        self.closed, self.interrupted = False, interrupted

    def json(self):
        return self.payload

    def iter_lines(self, **kwargs):
        for event in self.events:
            yield "data: " + json.dumps(event)
            yield ""
        if self.interrupted:
            raise requests.ConnectionError("fake interruption")

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.calls = []
        self.catalog = FakeResponse({"models": [{"slug": "gpt-6.1-sol", "display_name": "Sol", "visibility": "list"},
                                                 {"slug": "internal", "visibility": "hide"}]})
        self.inference = FakeResponse(events=[completed()])

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.catalog

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.inference


class ChatGPTClientTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.auth = Mock()
        self.auth.access_token.return_value = "fake-oauth-token"
        self.client = ChatGPTClient(auth=self.auth, session=self.session)

    def generate(self, **kwargs):
        return self.client.generate_json("gpt-6.1-sol", "same Mer input", "Mer and AI must remain distinct", {"type": "object"}, **kwargs)

    def test_success_uses_public_endpoint_supported_shape_and_terminal_output(self):
        budget = Mock()
        result = self.generate(before_request=budget)
        self.assertEqual(json.loads(result.text), {"portfolio_decisions": []})
        self.assertEqual(result.model_version, "gpt-6.1-sol-snapshot")
        self.assertEqual(result.usage["input_tokens"], 100)
        budget.assert_called_once_with()
        method, url, kwargs = self.session.calls[-1]
        self.assertEqual((method, url), ("post", RESOURCE + "/responses"))
        payload = kwargs["json"]
        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])
        self.assertIsInstance(payload["input"], list)
        self.assertEqual(payload["text"]["format"]["type"], "json_object")
        self.assertIn("Follow this exact portfolio JSON schema", payload["instructions"])
        self.assertFalse({"max_output_tokens", "temperature", "background", "previous_response_id", "metadata"} & payload.keys())
        self.assertFalse(kwargs["allow_redirects"])
        self.assertGreater(kwargs["timeout"][1], 30)
        self.assertTrue(self.session.inference.closed)

    def test_catalog_is_account_specific_cached_and_filters_visibility(self):
        self.assertEqual(self.client.list_models(), [{"slug": "gpt-6.1-sol", "display_name": "Sol"}])
        self.generate()
        self.generate()
        self.assertEqual(sum(method == "get" for method, _, _ in self.session.calls), 1)
        self.client.list_models(refresh=True)
        self.assertEqual(sum(method == "get" for method, _, _ in self.session.calls), 2)

    def test_model_unavailable_or_catalog_failure_uses_no_generation_budget(self):
        for payload, status in (({"models": []}, 200), ({"data": [{"id": "gpt-6.1-sol"}]}, 200), ({}, 401)):
            with self.subTest(payload=payload, status=status):
                self.session.catalog = FakeResponse(payload, status=status)
                self.client._models = None
                budget = Mock()
                with self.assertRaises(ChatGPTClientError):
                    self.generate(before_request=budget)
                budget.assert_not_called()
        self.assertFalse(any(method == "post" for method, _, _ in self.session.calls))

    def test_auth_missing_uses_no_generation_budget_or_network(self):
        self.auth.access_token.side_effect = ChatGPTAuthError("No account")
        budget = Mock()
        with self.assertRaises(ChatGPTAuthError):
            self.generate(before_request=budget)
        budget.assert_not_called()
        self.assertEqual(self.session.calls, [])

    def test_generation_http_failure_has_one_attempt_and_consumes_budget(self):
        self.session.inference = FakeResponse(status=429)
        budget = Mock()
        with self.assertRaisesRegex(ChatGPTClientError, "HTTP 429"):
            self.generate(before_request=budget)
        budget.assert_called_once()
        self.assertEqual(sum(method == "post" for method, _, _ in self.session.calls), 1)
        self.assertTrue(self.session.inference.closed)

    def test_failed_incomplete_and_usage_errors_discard_partial_text(self):
        for kind in ("response.failed", "response.incomplete", "error"):
            with self.subTest(kind=kind):
                self.session.inference = FakeResponse(events=[
                    {"type": "response.output_text.delta", "delta": '{"partial":'},
                    {"type": kind, "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "private"}}},
                ])
                with self.assertRaisesRegex(ChatGPTClientError, "subscription_sharing_usage_limit_exceeded") as error:
                    self.generate()
                self.assertNotIn("private", str(error.exception))
                self.assertTrue(self.session.inference.closed)

    def test_interrupted_or_uncompleted_stream_cannot_succeed(self):
        for interrupted in (False, True):
            self.session.inference = FakeResponse(events=[{"type": "response.output_text.delta", "delta": '{"partial": 1}'}], interrupted=interrupted)
            with self.assertRaises(ChatGPTClientError):
                self.generate()
            self.assertTrue(self.session.inference.closed)

    def test_malformed_terminal_output_is_rejected(self):
        for event in (
            {"type": "response.failed", "response": ["bad"]},
            {"type": "response.completed", "response": {"status": "completed", "output": "bad"}},
            {"type": "response.completed", "response": {"status": "completed", "output": [{"type": "message", "content": "bad"}]}},
            {"type": "response.completed", "response": {"status": "incomplete"}},
        ):
            with self.subTest(event=event):
                self.session.inference = FakeResponse(events=[event])
                with self.assertRaises(ChatGPTClientError):
                    self.generate()

    def test_exhausted_budget_and_budget_callback_stop_before_post(self):
        budget = Mock(side_effect=RuntimeError("request count exhausted"))
        with self.assertRaisesRegex(RuntimeError, "request count exhausted"):
            self.generate(before_request=budget)
        self.assertFalse(any(method == "post" for method, _, _ in self.session.calls))
        with self.assertRaises(ChatGPTClientError):
            self.generate(retry_budget_seconds=0)

    def test_sse_multiline_comments_and_eof(self):
        self.assertEqual(list(_events([b": heartbeat", b"event: response", b'data: {"a":', b"data: 1}", b"", b"data: [DONE]"])), ['{"a":\n1}', "[DONE]"])


if __name__ == "__main__":
    unittest.main()
