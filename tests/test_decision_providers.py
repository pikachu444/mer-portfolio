import gc
import json
import weakref
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import analyze
from decision_providers import TrackedGeminiClient, resolve_context_mode, resolve_provider


POSTS = [{"title": "공급", "date": "2026-10-05", "url": "https://blog.naver.com/ranto28/1", "content": "원문", "summary": "공급 변화"}]
DECISION = {"analysis_date": "2026-10-06", "run_type": "regular", "insights": [], "portfolio_decisions": [], "watchlist": []}


class Budget:
    def __init__(self, maximum):
        self.maximum = maximum
        self.requests = []

    def consume(self, provider, model):
        if len(self.requests) >= self.maximum:
            raise RuntimeError("generation request budget exhausted")
        self.requests.append((provider, model))


class FakeChatGPT:
    def __init__(self, texts):
        self.texts = iter(texts)
        self.generated = 0

    def generate_json(self, **kwargs):
        kwargs["before_request"]()
        self.generated += 1
        return SimpleNamespace(text=next(self.texts), model_version=kwargs["model"], usage={"input_tokens": 20, "output_tokens": 10}, latency_seconds=0.1)


class DecisionProviderTest(unittest.TestCase):
    def test_source_repair_identifies_target_direction_and_unknown_id_without_rewriting(self):
        row = {'code': 'AA', 'name': 'Alcoa', 'market': 'US', 'asset_type': 'stock', 'action': '보유',
               'decision_actor': 'AI', 'source_scope': 'source_named_security', 'linked_signal_ids': ['old', 'bad', 'missing']}
        state = {'portfolio': [{**row, 'linked_signal_ids': ['old'], 'origin_signal_ids': ['old']}]}
        events = [{'signal_id': 'bad', 'signal_type': 'MER_THESIS', 'direction': 'bearish',
                   'entity': {'code': 'BHP', 'name': 'BHP', 'market': 'US'}}]
        message = analyze._source_repair_feedback(SimpleNamespace(portfolio_decisions=[row]), state, events)
        self.assertIn('AA', message)
        self.assertIn('BHP', message)
        self.assertIn('bearish', message)
        self.assertIn('missing', message)
        self.assertIn('원문 URL', message)
        self.assertNotIn('"signal_id":"old"', message)
        self.assertEqual(events[0]['direction'], 'bearish')
        self.assertEqual(row['linked_signal_ids'], ['old', 'bad', 'missing'])

    def _review_exposure(self, *, proposed=8, previous=0, parents=None):
        row = {"asset_type": "stock", "market": "US", "code": "AA", "proposed_weight": proposed, "origin_signal_ids": ["inferred"]}
        state = {"portfolio": [{**row, "proposed_weight": previous}]} if previous else {"portfolio": []}
        events = [
            {"signal_id": "bad", "signal_type": "MER_THESIS", "direction": "bullish"},
            {"signal_id": "clean", "signal_type": "MER_THESIS", "direction": "bullish"},
            {"signal_id": "inferred", "signal_type": "AI_INFERRED", "parent_signal_ids": parents or ["bad"]},
        ]
        analyze._validate_direction_review_exposure(SimpleNamespace(portfolio_decisions=[row]), state, events, {"bad"})

    def test_mixed_direction_alone_cannot_justify_new_position(self):
        with self.assertRaisesRegex(ValueError, "方向|방향 검토"):
            self._review_exposure()

    def test_mixed_direction_alone_cannot_justify_weight_increase(self):
        with self.assertRaisesRegex(ValueError, "방향 검토"):
            self._review_exposure(previous=5, proposed=8)

    def test_independent_canonical_bullish_basis_can_support_increase(self):
        self._review_exposure(parents=["bad", "clean"])

    def test_mixed_direction_review_does_not_force_unchanged_holding_or_sale(self):
        self._review_exposure(previous=8, proposed=8)
        self._review_exposure(previous=8, proposed=5)

    def test_unrelated_raw_link_cannot_bypass_canonical_origins(self):
        row = {"asset_type": "stock", "market": "US", "code": "AA", "proposed_weight": 8, "origin_signal_ids": ["bad"], "linked_signal_ids": ["bad", "unrelated"]}
        events = [{"signal_id": signal_id, "signal_type": "MER_THESIS", "direction": "bullish"} for signal_id in ["bad", "unrelated"]]
        with self.assertRaisesRegex(ValueError, "방향 검토"):
            analyze._validate_direction_review_exposure(SimpleNamespace(portfolio_decisions=[row]), {"portfolio": []}, events, {"bad"})

    def test_defaults_keep_gemini_baseline(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_provider(), "gemini")
            self.assertEqual(resolve_context_mode(), "baseline")
        with self.assertRaises(ValueError):
            resolve_provider("automatic")

    def test_chatgpt_does_not_initialize_gemini(self):
        client = FakeChatGPT([json.dumps(DECISION)])
        budget = Budget(1)
        with patch("chatgpt_client.ChatGPTClient.from_env", return_value=client), patch.object(analyze, "_get_client") as gemini:
            result = analyze.analyze_posts_structured(POSTS, "2026-10-06", None, decision_provider="chatgpt", decision_model="account-model", request_budget=budget)
        gemini.assert_not_called()
        self.assertEqual(budget.requests, [("chatgpt", "account-model")])
        self.assertEqual(result.decision_provider, "chatgpt")
        self.assertEqual(result.request_metrics[0]["input_tokens"], 20)

    def test_correction_consumes_second_generation_request(self):
        wrong = {**DECISION, "analysis_date": "2026-10-05"}
        client = FakeChatGPT([json.dumps(wrong), json.dumps(DECISION)])
        budget = Budget(2)
        with patch("chatgpt_client.ChatGPTClient.from_env", return_value=client):
            result = analyze.analyze_posts_structured(POSTS, "2026-10-06", None, decision_provider="chatgpt", request_budget=budget)
        self.assertEqual(client.generated, 2)
        self.assertEqual(len(budget.requests), 2)
        self.assertEqual(len(result.request_metrics), 2)

    def test_correction_cannot_bypass_shared_budget(self):
        wrong = {**DECISION, "analysis_date": "2026-10-05"}
        client = FakeChatGPT([json.dumps(wrong), json.dumps(DECISION)])
        budget = Budget(1)
        with patch("chatgpt_client.ChatGPTClient.from_env", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "ChatGPT 투자 판단 보류.*budget exhausted"):
                analyze.analyze_posts_structured(POSTS, "2026-10-06", None, decision_provider="chatgpt", request_budget=budget)
        self.assertEqual(client.generated, 1)

    def test_gemini_transport_attempts_are_counted_not_token_checks(self):
        metrics = []
        budget = Budget(1)
        models = unittest.mock.Mock()
        models.generate_content.return_value = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=20))
        tracked = TrackedGeminiClient(SimpleNamespace(models=models), budget, metrics)
        tracked.models.count_tokens(model="gemini-3.5-flash", contents="request")
        self.assertEqual(budget.requests, [])
        tracked.models.generate_content(model="gemini-3.5-flash", contents="request", config=None)
        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            tracked.models.generate_content(model="gemini-3.5-flash", contents="request", config=None)
        models.generate_content.assert_called_once()
        self.assertEqual(metrics[0]["input_tokens"], 100)

    def test_gemini_proxy_keeps_real_sdk_transport_owner_alive(self):
        from google import genai
        # No network call or real key: only construct the actual SDK lifecycle.
        # Ignore execution-environment proxy settings for this local test.
        environment = {key: value for key, value in os.environ.items() if not key.lower().endswith('_proxy')}
        with patch.dict(os.environ, environment, clear=True):
            client = genai.Client(api_key='test-lifecycle-only', vertexai=False)
        reference = weakref.ref(client)
        tracked = TrackedGeminiClient(client, Budget(1), [])
        del client
        gc.collect()
        try:
            self.assertIsNotNone(reference(), 'SDK owner was collected and closed the transport')
            with patch.object(tracked.models._models, 'generate_content', return_value=SimpleNamespace(usage_metadata=None)) as generation:
                tracked.models.generate_content(model='test-model', contents='test')
                generation.assert_called_once()
        finally:
            alive = reference()
            if alive is not None:
                alive.close()

    def test_preview_is_network_free_and_unchanged_for_baseline(self):
        with patch.object(analyze, "_get_client") as client:
            preview = analyze.preview_decision_input(POSTS, "2026-10-06", None, context_mode="baseline")
        client.assert_not_called()
        expected = analyze.build_decision_user_message(context=analyze._structured_context(POSTS), analysis_date="2026-10-06", run_type="regular", current_state={})
        self.assertEqual(preview, expected)


if __name__ == "__main__":
    unittest.main()
