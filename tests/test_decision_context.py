import ast
import json
import unittest
from copy import deepcopy
from pathlib import Path

from decision_context import ambiguous_source_signal_ids, build_evidence_packet, compact_state_for_decision
from portfolio_provenance import prepare_post_signal_events


def event(signal_id, *, parents=None, name="현재 종목", code="CUR", direction="bullish", published="2026-10-01"):
    return {
        "signal_id": signal_id,
        "parent_signal_ids": parents or [],
        "entity": {"name": name, "code": code, "market": "US", "asset_type": "stock"},
        "published_date": published,
        "signal_type": "AI_INFERRED" if parents else "MER_THESIS",
        "direction": direction,
        "evidence_text": "원문 그대로의 위험과 전제 조건",
        "invalidation_conditions": ["원문 조건이 무효가 된 경우"],
        "thesis_id": "current-thesis",
    }


def active(**changes):
    row = {
        "name": "현재 종목", "code": "CUR", "market": "US", "asset_type": "stock",
        "action": "보유", "decision_date": "2026-10-01", "thesis_id": "current-thesis",
        "previous_weight": 5, "proposed_weight": 5,
        "origin_signal_ids": ["child"], "linked_signal_ids": [],
        "linked_insight_ids": ["needed"], "key_risks": ["아직 해결되지 않은 위험"],
        "invalidation_conditions": ["매출 가설 붕괴"],
        "evidence_posts": [{"url": "https://blog.naver.com/ranto28/123", "published_date": "2026-10-01"}],
    }
    return {**row, **changes}


def post(index=0, *, classification="DIRECTIONAL_THESIS", direction="수혜", quote="전제 조건이 유지되면 회사에 수혜가 있다."):
    return {
        "title": f"원문 {index}", "date": "2026-10-05",
        "url": f"https://blog.naver.com/ranto28/{1000 + index}",
        "content": f"반대 시나리오도 가능하다. {quote} 조건이 사라지면 다시 검토해야 한다.",
        "summary": "이미 저장된 요약", "summary_status": "ok",
        "signal_candidates": [{
            "exact_text": quote, "classification": classification, "entity_name": "현재 종목",
            "entity_type": "company", "direction": direction, "horizon_kind": "event",
            "catalysts": ["전제 조건 유지"], "invalidation_conditions": [],
        }],
    }


class DecisionContextTest(unittest.TestCase):
    def test_active_rows_and_full_source_fields_are_preserved(self):
        state = {"portfolio": [active()], "watchlist": [active(code="WATCH", status="재검토 필요")],
                 "signal_events": [event("child", parents=["parent"]), event("parent")],
                 "insights": [{"id": "needed", "summary": "현재 판단의 핵심 근거"}]}
        result = compact_state_for_decision(state)
        self.assertEqual(result["portfolio"], state["portfolio"])
        self.assertEqual(result["watchlist"], state["watchlist"])
        self.assertEqual(result["signal_events"], state["signal_events"])
        self.assertEqual(result["insights"], state["insights"])

    def test_signal_ancestors_are_retained_transitively(self):
        state = {"portfolio": [active()], "signal_events": [
            event("child", parents=["parent"]), event("parent", parents=["grandparent"]),
            event("grandparent", published="2020-01-01"),
            {**event("unrelated", name="다른 회사", code="OTHER"), "thesis_id": "unrelated-thesis"},
        ]}
        result = compact_state_for_decision(state)
        self.assertEqual([r["signal_id"] for r in result["signal_events"]], ["child", "parent", "grandparent"])

    def test_cycle_does_not_loop_and_missing_ancestor_is_visible(self):
        state = {"portfolio": [active()], "signal_events": [
            event("child", parents=["parent", "not-in-ledger"]), event("parent", parents=["child"]),
        ]}
        result = compact_state_for_decision(state)
        self.assertEqual(len(result["signal_events"]), 2)
        self.assertEqual(result["unavailable_referenced_signal_ids"], ["not-in-ledger"])

    def test_new_unlinked_bearish_counterevidence_is_preserved(self):
        state = {"portfolio": [active()], "signal_events": [
            event("child", published="2026-09-01"),
            event("new-risk", direction="bearish", published="2026-10-02"),
            event("old-unlinked", published="2020-01-01"),
        ]}
        result = compact_state_for_decision(state)
        self.assertEqual([r["signal_id"] for r in result["signal_events"]], ["child", "new-risk"])
        self.assertEqual(result["signal_events"][1]["direction"], "bearish")

    def test_latest_material_history_replaces_repeated_holds(self):
        latest_change = active(action="비중확대", previous_weight=3, proposed_weight=5, decision_date="2026-09-29")
        earlier_change = active(action="매수", previous_weight=0, proposed_weight=3, decision_date="2026-09-01")
        state = {"portfolio": [active()], "decision_history": [earlier_change, latest_change, active(), active()],
                 "signal_events": [event("child")]}
        result = compact_state_for_decision(state)
        self.assertEqual(result["decision_history"], [latest_change])
        self.assertEqual(result["decision_history"][0]["key_risks"], latest_change["key_risks"])

    def test_unclosed_risk_and_pending_admin_are_not_discarded(self):
        unresolved = active(code="OTHER", risk_status="open", origin_signal_ids=["risk"])
        pending = {"name": "관리자 검토", "code": "ADMIN", "queue_status": "pending_admin", "linked_signal_ids": ["admin"]}
        state = {"portfolio": [active()], "closed_positions": [unresolved],
                 "decision_history": [unresolved], "admin_review_queue": [pending],
                 "signal_events": [event("child"), event("risk", code="OTHER"), event("admin", code="ADMIN")]}
        result = compact_state_for_decision(state)
        self.assertEqual(result["closed_positions"], [unresolved])
        self.assertEqual(result["decision_history"], [unresolved])
        self.assertEqual(result["admin_review_queue"], [pending])
        self.assertEqual(len(result["signal_events"]), 3)

    def test_irrelevant_closed_history_is_dropped_and_active_prior_exit_remains(self):
        relevant = active(closed_date="2026-08-01", close_reason="과거 무효화")
        irrelevant = active(code="OLD", thesis_id="old-thesis", closed_date="2020-01-01")
        state = {"portfolio": [active()], "closed_positions": [relevant, irrelevant], "signal_events": [event("child")]}
        result = compact_state_for_decision(state)
        self.assertEqual(result["closed_positions"], [relevant])

    def test_compaction_and_evidence_packet_do_not_mutate_input(self):
        state = {"portfolio": [active()], "signal_events": [event("child")]}
        posts = [post()]
        frozen_state, frozen_posts = deepcopy(state), deepcopy(posts)
        result = compact_state_for_decision(state)
        packet = build_evidence_packet(posts, state)
        result["portfolio"][0]["key_risks"].append("출력만 변경")
        packet["source_posts"][0]["signals"][0]["quote_contexts"].append({"before": "출력만 변경"})
        self.assertEqual(state, frozen_state)
        self.assertEqual(posts, frozen_posts)

    def test_action_index_matches_existing_guard_and_mention_only_is_watch_only(self):
        packet = build_evidence_packet([
            post(0), post(1, direction="피해"), post(2, classification="MENTION_ONLY"), post(3, direction="중립"),
        ], {})
        rows = [p["signals"][0] for p in packet["source_posts"]]
        self.assertEqual(rows[0]["compatible_actions"], ["매수", "비중확대", "보유"])
        self.assertEqual(rows[1]["compatible_actions"], ["비중축소", "매도"])
        self.assertEqual(rows[2]["compatible_actions"], [])
        self.assertEqual(rows[3]["compatible_actions"], [])
        self.assertTrue(all(r["signal_id"].startswith("sig_") for r in rows))

    def test_mixed_bond_rates_price_and_feed_cost_directions_need_review(self):
        sources = [post(0, direction="수혜(국채금리 상승은 채권 가격 하락)"), post(1, direction="피해/비용 상승")]
        frozen = deepcopy(sources)
        prepared, _ = prepare_post_signal_events(sources, created_at="2026-10-06", model_id="cached-summary")
        flagged = ambiguous_source_signal_ids(prepared)
        expected = {source["signal_candidates"][0]["signal_id"] for source in prepared}
        self.assertEqual(flagged, expected)
        self.assertEqual(ambiguous_source_signal_ids(sources), expected)
        rows = [source["signals"][0] for source in build_evidence_packet(prepared, {})["source_posts"]]
        self.assertTrue(all(row["requires_direction_review"] for row in rows))
        # Expose the old host interpretation; do not silently rewrite its policy.
        self.assertTrue(all(row["host_direction"] == "bullish" for row in rows))
        self.assertEqual(sources, frozen)

    def test_ordinary_bullish_and_bearish_directions_do_not_need_review(self):
        sources = [post(0, direction="수혜"), post(1, direction="피해"), post(2, direction="bullish"), post(3, direction="bearish")]
        self.assertEqual(ambiguous_source_signal_ids(sources), set())
        self.assertFalse(any(row["requires_direction_review"] for source in build_evidence_packet(sources, {})["source_posts"] for row in source["signals"]))

    def test_conditional_quote_with_both_cues_does_not_flag_single_direction_label(self):
        source = post(quote="비용 상승은 회사에 피해지만 공급 제한이 유지되면 수혜를 볼 수 있다.", direction="수혜")
        self.assertEqual(ambiguous_source_signal_ids([source]), set())
        self.assertFalse(build_evidence_packet([source], {})["source_posts"][0]["signals"][0]["requires_direction_review"])

    def test_single_cue_negation_is_not_claimed_to_be_semantically_solved(self):
        source = post(direction="수혜 가능성 없음")
        self.assertEqual(ambiguous_source_signal_ids([source]), set())
        self.assertEqual(build_evidence_packet([source], {})["investment_bridge_status"], "REQUIRES_REVIEW")

    def test_mismatched_source_quote_is_visible_and_cannot_supply_action_support(self):
        source = post()
        source["content"] = "이 원문에는 인용된 문장이 없다."
        row = build_evidence_packet([source], {})["source_posts"][0]["signals"][0]
        self.assertFalse(row["quote_verified_in_cached_source"])
        self.assertEqual(row["compatible_actions"], [])
        self.assertNotIn("exact_text", row)

    def test_quote_context_keeps_conditional_and_opposing_scenarios_without_inventing_conditions(self):
        source = post()
        packet = build_evidence_packet([source], {})
        row = packet["source_posts"][0]["signals"][0]
        snippet = row["quote_contexts"][0]
        self.assertIn("반대 시나리오", snippet["before"])
        self.assertIn("조건이 사라지면", snippet["after"])
        self.assertIn(source["signal_candidates"][0]["exact_text"], source["content"])
        self.assertNotIn("exact_text", row)
        self.assertEqual(row["invalidation_status"], "UNKNOWN")
        self.assertNotIn("invalidation_conditions", row)
        self.assertEqual(packet["investment_bridge_status"], "REQUIRES_REVIEW")
        self.assertEqual(packet["market_data_status"], "NOT_PROVIDED_BY_THIS_INDEX")

    def test_all_quote_occurrences_and_high_recent_post_volume_are_retained(self):
        sources = [post(i) for i in range(100)]
        sources[0]["content"] += " " + sources[0]["signal_candidates"][0]["exact_text"]
        packet = build_evidence_packet(sources, {})
        self.assertEqual(len(packet["source_posts"]), 100)
        self.assertEqual(len(packet["source_posts"][0]["signals"][0]["quote_contexts"]), 2)
        self.assertTrue(all(source["signals"] for source in packet["source_posts"]))

    def test_real_cached_state_keeps_complete_active_lineage(self):
        root = Path(__file__).resolve().parents[1]
        state = json.loads((root / "output" / "portfolio_state.json").read_text(encoding="utf-8"))
        # Read the old function without importing Gemini or performing API setup.
        module = ast.parse((root / "analyze.py").read_text(encoding="utf-8"))
        function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "_compact_state_for_inference")
        scope = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "baseline-context", "exec"), scope)
        baseline = scope["_compact_state_for_inference"](state)
        result = compact_state_for_decision(state)
        self.assertEqual(result["portfolio"], state["portfolio"])
        self.assertEqual(result["watchlist"], state["watchlist"])
        all_ids = {r["signal_id"] for r in result["signal_events"]}
        for row in result["signal_events"]:
            self.assertTrue(set(row.get("parent_signal_ids", [])) <= all_ids)
        for row in state["portfolio"] + state["watchlist"]:
            self.assertTrue(set(row.get("origin_signal_ids", []) + row.get("linked_signal_ids", [])) <= all_ids)
        self.assertTrue(all(row in result["admin_review_queue"] for row in state["admin_review_queue"] if row["queue_status"] == "pending_admin"))
        self.assertGreaterEqual(len(result["signal_events"]), len(baseline["signal_events"]))


if __name__ == "__main__":
    unittest.main()
