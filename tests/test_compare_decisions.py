import json
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import compare_decisions as comparison
from portfolio_schema import parse_analysis_decision
from test_portfolio_schema import decision, insight


AS_OF = "2026-06-01"


def cached_post(**overrides):
    post = {
        "title": "기니, 중국을 건드리나?", "date": "2026-05-27",
        "url": "https://blog.naver.com/ranto28/123",
        "content": "Alcoa가 공급 제한의 수혜를 받을 수 있다.",
        "summary": "Alcoa 공급 제한 수혜 가능성", "summary_version": 4,
        "summary_status": "ok", "investment_relevant": True,
        "analysis_status": "pending", "summary_model_version": "cached-flash-lite",
        "signal_candidates": [{
            "exact_text": "Alcoa가 공급 제한의 수혜를 받을 수 있다.",
            "classification": "DIRECTIONAL_THESIS", "entity_name": "Alcoa",
            "entity_type": "company", "code": "AA", "market": "US",
            "direction": "수혜", "horizon_kind": "cyclical",
            "catalysts": ["공급 제한"], "invalidation_conditions": ["공급 정상화"],
        }],
    }
    post.update(overrides)
    return post


def empty_state():
    return {
        "schema_version": "2.0", "portfolio": [], "watchlist": [],
        "closed_positions": [], "decision_history": [], "insights": [],
        "last_rebalanced_date": "2026-05-14",
    }


def response_for(posts, provider="gemini", weight=8.0):
    item = decision(decision_date=AS_OF, proposed_weight=weight)
    item["linked_signal_ids"] = [posts[0]["signal_candidates"][0]["signal_id"]]
    return SimpleNamespace(
        decision=parse_analysis_decision({
            "analysis_date": AS_OF, "run_type": "regular",
            "insights": [insight()], "portfolio_decisions": [item], "watchlist": [],
        }),
        decision_model_version=f"{provider}-mock-model",
        request_metrics=[{"provider": provider, "model": "mock", "input_tokens": 100,
                          "output_tokens": 50, "total_tokens": 150, "latency_seconds": 0.1}],
    )


class ComparisonTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "operating"
        self.source.mkdir()
        self.destination = self.root / "experiment"
        self.write_sources([cached_post()], empty_state())

    def write_sources(self, posts, state):
        (self.source / "posts_db.json").write_text(json.dumps(posts), encoding="utf-8")
        (self.source / "portfolio_state.json").write_text(json.dumps(state), encoding="utf-8")
        (self.source / "model_portfolio_ledger.json").write_text('{"positions": []}', encoding="utf-8")
        (self.source / "scheduled_delivery_receipt.json").write_text('{"accepted": true}', encoding="utf-8")

    def run_comparison(self, **options):
        options.setdefault("preview_fn", lambda posts, date, state, kind, context: json.dumps({
            "posts": posts, "state": state, "mode": context,
        }, ensure_ascii=False))
        return comparison.run_comparison(
            source_dir=self.source, destination=self.destination,
            analysis_date=AS_OF, **options,
        )

    def test_default_dry_run_constructs_context_with_zero_generation_and_unchanged_files(self):
        analyze_fn = Mock(side_effect=AssertionError("live generation forbidden"))
        before = comparison.snapshot_hashes(self.source)
        with patch("analyze._get_client", side_effect=AssertionError("SDK initialization forbidden")):
            result = self.run_comparison(analyze_fn=analyze_fn)
        analyze_fn.assert_not_called()
        self.assertEqual(result["request_budget"]["generation_attempts"], 0)
        self.assertEqual([attempt["status"] for attempt in result["attempts"]], ["dry-run"] * 3)
        self.assertEqual(result["source_quote_audit"]["matched"], 1)
        self.assertEqual(comparison.snapshot_hashes(self.source), before)
        self.assertTrue(result["operating_state_unchanged"])
        self.assertFalse(result["limits"]["returns_verified"])

    def test_real_preview_helper_is_network_free_without_api_keys(self):
        with patch("analyze._get_client", side_effect=AssertionError("Gemini SDK initialization forbidden")), \
             patch("analyze._count_tokens", side_effect=AssertionError("live token counting forbidden")):
            result = comparison.run_comparison(
                source_dir=self.source, destination=self.destination, analysis_date=AS_OF,
            )
        self.assertEqual(result["request_budget"]["generation_attempts"], 0)
        self.assertTrue(all(row["request_preview_characters"] > 0 for row in result["attempts"]))
        self.assertEqual(len(result["templates"]["decision_schema_sha256"]), 64)

    def test_live_variants_use_identical_snapshots_even_if_provider_mutates_its_copy(self):
        observed = []
        def analyze_fn(posts, date, state, **options):
            observed.append(deepcopy({"posts": posts, "date": date, "state": state}))
            provider = options["decision_provider"]
            options["request_budget"].consume(provider, "mock")
            result = response_for(posts, provider, 9.0 if provider == "chatgpt" else 8.0)
            options["decision_validator"](result.decision)
            posts[0]["summary"] = "provider-local mutation"
            state["portfolio"] = [{"name": "provider-local mutation"}]
            return result

        result = self.run_comparison(live=True, analyze_fn=analyze_fn)
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[1], observed[2])
        self.assertEqual(len({item["input_sha256"] for item in result["attempts"]}), 1)
        self.assertEqual(result["request_budget"]["generation_attempts"], 3)
        self.assertEqual([item["status"] for item in result["attempts"]], ["valid"] * 3)
        self.assertEqual(result["attempts"][2]["changes_vs_first_valid_gemini_baseline"][0]["weight_change_pp"], 1.0)
        self.assertEqual(result["attempts"][2]["successful_generation_responses"], 1)
        self.assertTrue(result["operating_state_unchanged"])

    def test_retry_budget_is_consumed_before_transport_and_skips_remaining_variants(self):
        transports = []
        def analyze_fn(posts, date, state, **options):
            for attempt in range(2):
                options["request_budget"].consume(options["decision_provider"], "mock")
                transports.append(attempt)
            return response_for(posts)

        result = self.run_comparison(live=True, max_requests=1, analyze_fn=analyze_fn)
        self.assertEqual(transports, [0])
        self.assertEqual(result["request_budget"]["generation_attempts"], 1)
        self.assertEqual([row["status"] for row in result["attempts"]], ["rejected", "skipped-budget", "skipped-budget"])
        self.assertTrue(result["operating_state_unchanged"])

    def test_budget_is_thread_safe(self):
        budget = comparison.RequestBudget(2)
        accepted = []
        def consume():
            try:
                budget.consume("gemini", "model")
                accepted.append(True)
            except comparison.RequestBudgetExceeded:
                pass
        threads = [threading.Thread(target=consume) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(accepted), 2)
        self.assertEqual(budget.summary()["remaining"], 0)

    def test_rejected_decision_retains_successful_transport_usage_when_provider_records_it(self):
        def analyze_fn(posts, date, state, **options):
            budget = options["request_budget"]
            budget.consume("gemini", "mock")
            budget.record_response({"provider": "gemini", "model": "mock", "input_tokens": 200,
                                    "output_tokens": 100, "authorization": "must-not-save"})
            raise ValueError("decision validation failed")
        result = self.run_comparison(live=True, variants=["gemini-baseline"], analyze_fn=analyze_fn)
        attempt = result["attempts"][0]
        self.assertEqual(attempt["status"], "rejected")
        self.assertEqual(attempt["generation_attempts"], 1)
        self.assertEqual(attempt["successful_generation_responses"], 1)
        self.assertEqual(attempt["request_metrics"][0]["input_tokens"], 200)
        self.assertNotIn("authorization", attempt["request_metrics"][0])

    def test_failed_transport_is_not_counted_as_successful_response(self):
        def analyze_fn(posts, date, state, **options):
            budget = options["request_budget"]
            budget.consume("gemini", "mock")
            budget.record_response({"provider": "gemini", "model": "mock", "status": "failed"})
            raise RuntimeError("transport unavailable")
        result = self.run_comparison(live=True, variants=["gemini-baseline"], analyze_fn=analyze_fn)
        attempt = result["attempts"][0]
        self.assertEqual(attempt["generation_attempts"], 1)
        self.assertEqual(attempt["successful_generation_responses"], 0)
        self.assertEqual(attempt["request_metrics"][0]["status"], "failed")

    def test_local_request_cap_is_not_retried_as_a_gemini_quota_error(self):
        import gemini_utils
        from decision_providers import TrackedGeminiClient
        models = Mock()
        tracked = TrackedGeminiClient(SimpleNamespace(models=models), comparison.RequestBudget(0), [])
        with patch.object(gemini_utils, "wait_for_model_slot"), \
             patch.object(gemini_utils, "_model_slot_wait_seconds", return_value=0), \
             patch.object(gemini_utils.time, "sleep") as sleep:
            with self.assertRaises(comparison.RequestBudgetExceeded):
                gemini_utils.generate_content_with_retry(
                    client=tracked, model="gemini-3.5-flash", contents="request", config=None,
                    max_retries=3, retry_budget_seconds=180,
                )
        models.generate_content.assert_not_called()
        sleep.assert_not_called()

    def test_invalid_cached_quote_blocks_all_live_calls(self):
        post = cached_post(content="원문에 없는 문장만 있다.")
        self.write_sources([post], empty_state())
        generation = Mock()
        result = self.run_comparison(live=True, analyze_fn=generation)
        generation.assert_not_called()
        self.assertEqual(result["source_quote_audit"]["matched"], 0)
        self.assertEqual([row["status"] for row in result["attempts"]], ["blocked-input"] * 3)

    def test_regular_scope_keeps_pending_current_summary_and_excludes_future(self):
        cached = [cached_post(), cached_post(date="2026-06-02"),
                  cached_post(analysis_status="completed"), cached_post(summary_version=2)]
        selected, metrics = comparison.select_cached_posts(cached, empty_state(), AS_OF, "regular")
        self.assertEqual(len(selected), 1)
        self.assertEqual(metrics["future_posts_excluded"], 1)
        self.assertEqual(metrics["blocked_posts"], [])

    def test_rebalance_waits_for_legacy_source_upgrade_in_complete_window(self):
        self.write_sources([cached_post(), cached_post(summary_version=2)], empty_state())
        generation = Mock()
        result = self.run_comparison(live=True, run_type="rebalance", analyze_fn=generation)
        generation.assert_not_called()
        self.assertEqual(len(result["selection"]["blocked_posts"]), 1)

    def test_current_state_cannot_be_replayed_before_its_decision_date(self):
        state = empty_state()
        state["decision_history"] = [{"decision_date": "2026-06-02"}]
        with self.assertRaisesRegex(ValueError, "historical snapshot"):
            comparison.select_cached_posts([cached_post()], state, AS_OF, "regular")

    def test_future_source_in_state_is_lookahead_but_future_expiry_is_not(self):
        state = empty_state()
        state["signal_events"] = [{"published_date": "2026-06-02"}]
        with self.assertRaisesRegex(ValueError, "historical snapshot"):
            comparison.select_cached_posts([cached_post()], state, AS_OF, "regular")
        state["signal_events"] = [{"published_date": "2026-05-27", "created_at": "2026-05-28T09:00:00+09:00"}]
        state["watchlist"] = [{"expiry_date": "2026-07-01"}]
        selected, _ = comparison.select_cached_posts([cached_post()], state, AS_OF, "regular")
        self.assertEqual(len(selected), 1)

    def test_bearish_source_cannot_validate_a_new_long(self):
        post = cached_post()
        post["signal_candidates"][0]["direction"] = "피해"
        self.write_sources([post], empty_state())
        def analyze_fn(posts, date, state, **options):
            options["request_budget"].consume(options["decision_provider"], "mock")
            return response_for(posts)
        result = self.run_comparison(live=True, variants=["gemini-baseline"], analyze_fn=analyze_fn)
        self.assertEqual(result["attempts"][0]["status"], "rejected")
        self.assertIn("verified directional", result["attempts"][0]["error"])

    def test_actual_focused_pipeline_can_correct_unverified_ief_within_shared_request_cap(self):
        post = cached_post()
        post["signal_candidates"][0]["direction"] = "피해"
        self.write_sources([post], empty_state())
        prepared, _ = comparison.prepare_post_signal_events([post], created_at=AS_OF, model_id="cached")
        bad = decision(
            name="iShares 7-10 Year Treasury Bond ETF", code="IEF", asset_type="etf",
            decision_date=AS_OF, source_scope="sector_only", source_mentioned=False,
            basis="섹터 분석", linked_signal_ids=[prepared[0]["signal_candidates"][0]["signal_id"]],
        )
        wrong = {"analysis_date": AS_OF, "run_type": "regular", "insights": [insight()],
                 "portfolio_decisions": [bad], "watchlist": []}
        corrected = {"analysis_date": AS_OF, "run_type": "regular", "insights": [],
                     "portfolio_decisions": [], "watchlist": []}
        for allowance, expected_status, expected_requests in [(1, "rejected", 1), (2, "valid", 2)]:
            with self.subTest(allowance=allowance):
                calls = []
                def generate_json(**options):
                    options["before_request"]()
                    calls.append(options["user_message"])
                    payload = wrong if len(calls) == 1 else corrected
                    return SimpleNamespace(
                        text=json.dumps(payload), model_version="mock-chatgpt", usage={}, latency_seconds=0.01,
                    )
                client = SimpleNamespace(generate_json=generate_json)
                with patch("chatgpt_client.ChatGPTClient.from_env", return_value=client):
                    result = self.run_comparison(live=True, variants=["chatgpt-focused"], max_requests=allowance)
                self.assertEqual(result["attempts"][0]["status"], expected_status)
                self.assertEqual(result["request_budget"]["generation_attempts"], expected_requests)
                self.assertEqual(len(calls), expected_requests)
                self.assertTrue(result["operating_state_unchanged"])
                if allowance == 2:
                    self.assertIn("검증 오류", calls[1])
                    self.assertEqual(result["attempts"][0]["decision"]["portfolio_decisions"], [])

    def test_state_mutation_is_detected_and_artifact_does_not_claim_success(self):
        def analyze_fn(posts, date, state, **options):
            options["request_budget"].consume("gemini", "mock")
            (self.source / "scheduled_delivery_receipt.json").write_text("{}")
            return response_for(posts)
        result = self.run_comparison(live=True, variants=["gemini-baseline"], analyze_fn=analyze_fn)
        self.assertFalse(result["operating_state_unchanged"])
        self.assertEqual(result["modified_operating_files"], ["scheduled_delivery_receipt.json"])

    def test_external_snapshot_also_protects_repository_operating_output(self):
        operating = self.root / "output"
        operating.mkdir()
        ledger = operating / "model_portfolio_ledger.json"
        ledger.write_text('{"positions": []}')
        def analyze_fn(posts, date, state, **options):
            options["request_budget"].consume("gemini", "mock")
            ledger.write_text("{}")
            return response_for(posts)
        with patch.object(comparison, "REPOSITORY_ROOT", self.root):
            result = self.run_comparison(live=True, variants=["gemini-baseline"], analyze_fn=analyze_fn)
        self.assertFalse(result["operating_state_unchanged"])
        self.assertEqual(result["modified_operating_files"], ["repository_output/model_portfolio_ledger.json"])

    def test_output_destination_cannot_be_inside_operating_state(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            comparison.run_comparison(source_dir=self.source, destination=self.source / "compare", analysis_date=AS_OF)
        self.assertFalse((self.source / "compare").exists())

    def test_provider_errors_redact_credentials(self):
        generation = Mock(side_effect=RuntimeError("access_token=secret-value Bearer secret-token sk-example-secret123"))
        result = self.run_comparison(live=True, variants=["chatgpt-focused"], analyze_fn=generation)
        error = result["attempts"][0]["error"]
        self.assertNotIn("secret-value", error)
        self.assertNotIn("secret-token", error)
        self.assertNotIn("sk-example", error)
        self.assertNotIn("raw_response", result["attempts"][0])


if __name__ == "__main__":
    unittest.main()
