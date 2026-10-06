"""Optional decision context, with source lineage and current risks intact.

These helpers create inference views. They never edit persisted portfolio state,
choose securities, change weights, or replace the normal decision validator.
"""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from typing import Any, Iterable

from portfolio_provenance import (
    _direction_matches_action,
    _iso_date,
    prepare_post_signal_events,
)


CONTEXT_VERSION = "focused-source-v2"
_SIGNAL_REFERENCE_KEYS = {"origin_signal_ids", "linked_signal_ids", "parent_signal_ids"}
_UNRESOLVED_STATUSES = {
    "open", "pending", "pending_admin", "unresolved", "active", "재검토 필요",
}
_ACTIONS = ("매수", "비중확대", "보유", "비중축소", "매도")
# Match the existing host normalizer's cues without changing its precedence.
# This detects an unresolved label, not the correct beneficiary or direction.
_POSITIVE_DIRECTION_CUES = ("positive", "bull", "수혜", "긍정", "상승", "매수", "보유", "확대")
_NEGATIVE_DIRECTION_CUES = ("negative", "bear", "피해", "부정", "하락", "매도", "축소", "회피")


def _rows(state: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return list(state.get(key, []) or [])


def _identity(row: dict[str, Any]) -> tuple[str, str, str]:
    code = str(row.get("code") or "").strip().upper()
    name = re.sub(r"\s+", "", str(row.get("name") or "")).casefold()
    return (
        str(row.get("asset_type") or ""),
        str(row.get("market") or "").strip().upper(),
        code or name,
    )


def _reference_ids(value: Any) -> set[str]:
    """Read references recursively, including references in nested risk records."""
    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _SIGNAL_REFERENCE_KEYS and isinstance(item, list):
                result.update(str(ref) for ref in item if str(ref))
            result.update(_reference_ids(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_reference_ids(item))
    return result


def _unresolved(row: dict[str, Any]) -> bool:
    return any(
        str(row.get(key) or "").strip().lower() in _UNRESOLVED_STATUSES
        for key in ("queue_status", "review_status", "risk_status", "status")
    )


def _is_material_decision(row: dict[str, Any]) -> bool:
    if str(row.get("action") or "") in {"매수", "매도", "비중확대", "비중축소"}:
        return True
    previous = row.get("previous_weight")
    proposed = row.get("proposed_weight")
    return previous is not None and proposed is not None and previous != proposed


def _latest_rows(rows: Iterable[dict[str, Any]], key) -> list[dict[str, Any]]:
    """Select existing rows deterministically; never merge/rewrite their fields."""
    selected: dict[Any, tuple[str, int, dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        row_key = key(row)
        stamp = str(row.get("closed_date") or row.get("decision_date") or "")
        current = selected.get(row_key)
        if current is None or (stamp, index) >= current[:2]:
            selected[row_key] = (stamp, index, row)
    return [item[2] for item in sorted(selected.values(), key=lambda item: item[1])]


def compact_state_for_decision(current_state: dict[str, Any] | None) -> dict[str, Any]:
    """Preserve live rows and source closure; remove unrelated closed/hold history.

All active portfolio and watchlist fields are copied verbatim. The historical
view retains the latest material decision for each current identity, unresolved
review rows, and relevant prior closures. Unclosed rows in ``closed_positions``
are always retained rather than assumed to be safe to discard. Persisted state
and the append-only event ledger are never changed by this view.
    """
    if not current_state:
        return {}
    state = deepcopy(current_state)
    portfolio = _rows(state, "portfolio")
    watchlist = _rows(state, "watchlist")
    active = portfolio + watchlist
    active_keys = {_identity(row) for row in active}
    active_theses = {str(row.get("thesis_id") or "") for row in active} - {""}

    history = _rows(state, "decision_history")
    material_history = _latest_rows(
        (row for row in history if _identity(row) in active_keys and _is_material_decision(row)),
        _identity,
    )
    # Explicit unresolved review records are information, not closed history.
    retained_history = [row for row in history if _unresolved(row) or row in material_history]
    retained_history = _latest_rows(
        retained_history,
        lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )

    closed = _rows(state, "closed_positions")
    relevant_closed = _latest_rows(
        (
            row for row in closed
            if row.get("closed_date") and (
                _identity(row) in active_keys
                or str(row.get("thesis_id") or "") in active_theses
                or _unresolved(row)
            )
        ),
        lambda row: (_identity(row), str(row.get("thesis_id") or "")),
    )
    retained_closed = [row for row in closed if not row.get("closed_date") or row in relevant_closed]
    # Keep all pending reviews, including assets absent from the active list.
    pending_reviews = [row for row in _rows(state, "admin_review_queue") if _unresolved(row)]
    insights = _rows(state, "insights")

    roots = _reference_ids(active + retained_history + retained_closed + pending_reviews + insights)
    events = _rows(state, "signal_events")
    event_by_id = {str(row.get("signal_id") or ""): row for row in events}
    # New, unlinked counterevidence for an active identity must remain visible.
    # The lower bound is the actual current decision date, not an invented TTL.
    for row in active:
        since = str(row.get("decision_date") or "")
        for event in events:
            entity = event.get("entity") or {}
            same_identity = (
                _identity(entity) == _identity(row)
                or (
                    re.sub(r"\s+", "", str(entity.get("name") or "")).casefold()
                    == re.sub(r"\s+", "", str(row.get("name") or "")).casefold()
                    and bool(row.get("name"))
                )
            )
            same_thesis = bool(row.get("thesis_id")) and row.get("thesis_id") == event.get("thesis_id")
            if (same_identity or same_thesis) and str(event.get("published_date") or "") >= since:
                roots.add(str(event.get("signal_id") or ""))

    wanted = set(roots)
    todo = list(roots)
    while todo:
        signal_id = todo.pop()
        event = event_by_id.get(signal_id)
        if event is None:
            continue
        for parent in event.get("parent_signal_ids", []) or []:
            parent = str(parent)
            if parent and parent not in wanted:
                wanted.add(parent)
                todo.append(parent)

    view = {
        "schema_version": state.get("schema_version"),
        "portfolio": portfolio,
        "watchlist": watchlist,
        "closed_positions": retained_closed,
        "decision_history": retained_history,
        "signal_events": [row for row in events if row.get("signal_id") in wanted],
        "insights": insights,
        "last_watchlist_changes": deepcopy(state.get("last_watchlist_changes", {})),
        "last_rebalanced_date": state.get("last_rebalanced_date"),
        "admin_review_queue": pending_reviews,
    }
    unavailable = sorted(wanted - event_by_id.keys())
    if unavailable:
        view["unavailable_referenced_signal_ids"] = unavailable
    return view


def _normalize_source(text: Any) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"[\u200b-\u200f\u2028\u2029\ufeff\u00ad]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _requires_direction_review(direction: Any) -> bool:
    """Flag mixed cues in the direction label only, not in conditional quotes.

    Negation and beneficiary semantics require a separate source review. For
    example, a positive cue followed by '없음' is not solved by this detector.
    """
    value = _normalize_source(direction).lower()
    return (
        any(cue in value for cue in _POSITIVE_DIRECTION_CUES)
        and any(cue in value for cue in _NEGATIVE_DIRECTION_CUES)
    )


def ambiguous_source_signal_ids(posts: Iterable[dict[str, Any]]) -> set[str]:
    """Return stable host IDs with mixed direction labels, without editing posts.

    The caller can restrict additional exposure relying on unresolved evidence.
    This helper neither relabels a source as bearish nor orders existing holdings
    to be sold. Already host-prepared candidates produce the same immutable IDs.
    """
    result: set[str] = set()
    for post in posts:
        if not any(_requires_direction_review(candidate.get("direction")) for candidate in post.get("signal_candidates", []) or []):
            continue
        prepared, _ = prepare_post_signal_events(
            [post],
            created_at=_iso_date(post.get("date") or post.get("published_date")),
            model_id=str(post.get("summary_model_id") or "cached-summary"),
        )
        result.update(
            str(candidate["signal_id"])
            for candidate in prepared[0].get("signal_candidates", []) or []
            if _requires_direction_review(candidate.get("direction"))
        )
    return result


def _quote_contexts(source: str, quote: str, *, surrounding_characters: int = 220) -> list[dict[str, Any]]:
    """Expose both sides of every quote; the original context contains the quote.

    Fragments are normalized source slices, never paraphrases. The character
    limit controls display size and is not a trading horizon or decision rule.
    """
    source = _normalize_source(source)
    quote = _normalize_source(quote)
    if not quote:
        return []
    result: list[dict[str, Any]] = []
    start = 0
    while True:
        offset = source.find(quote, start)
        if offset < 0:
            return result
        lo = max(0, offset - surrounding_characters)
        hi = min(len(source), offset + len(quote) + surrounding_characters)
        result.append({"before": source[lo:offset], "after": source[offset + len(quote):hi]})
        start = offset + len(quote)


def build_evidence_packet(posts: Iterable[dict[str, Any]], current_state: dict[str, Any] | None) -> dict[str, Any]:
    """Add a source-derived action index without replacing original post context.

Compatibility describes the existing host guard, not a recommendation. Original
direction strings and nearby source text stay visible so a conditional scenario
or a mismatched beneficiary is not mistaken for a validated investment thesis.
No ticker, market valuation, target weight, or new trading threshold is created.
    """
    post_list = deepcopy(list(posts))
    # The original state/post context is authoritative; this is only an index.
    _ = current_state
    sources: list[dict[str, Any]] = []
    for post in post_list:
        prepared, events = prepare_post_signal_events(
            [post],
            created_at=_iso_date(post.get("date") or post.get("published_date")),
            model_id=str(post.get("summary_model_id") or "cached-summary"),
        )
        candidates = prepared[0].get("signal_candidates", []) or []
        # A source may contain duplicate candidates for the same immutable ID.
        event_by_id = {event["signal_id"]: event for event in events}
        signal_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            event = event_by_id.get(str(candidate.get("signal_id") or ""))
            if event is None or event["signal_id"] in seen:
                continue
            seen.add(event["signal_id"])
            source = str(post.get("content") or "")
            quote = event["evidence_text"]
            contexts = _quote_contexts(source, quote) if source else []
            quote_verified = bool(contexts) if source else None
            allowed = []
            if event["signal_type"] in {"MER_DIRECT", "MER_THESIS"} and quote_verified is not False:
                allowed = [action for action in _ACTIONS if _direction_matches_action({"action": action}, event)]
            signal_rows.append({
                "signal_id": event["signal_id"],
                "classification": candidate.get("classification"),
                "signal_type": event["signal_type"],
                "entity": deepcopy(event["entity"]),
                "original_direction": candidate.get("direction"),
                "host_direction": event["direction"],
                "compatible_actions": allowed,
                "requires_direction_review": _requires_direction_review(candidate.get("direction")),
                "quote_verified_in_cached_source": quote_verified,
                "quote_contexts": contexts,
                "invalidation_status": "SOURCE_EXPLICIT" if event["invalidation_conditions"] else "UNKNOWN",
            })
        sources.append({
            "post_url": post.get("url"),
            "published_date": post.get("date") or post.get("published_date"),
            "signals": signal_rows,
        })
    return {
        "context_version": CONTEXT_VERSION,
        "action_index_rules": [
            "이 목록은 기존 호스트의 방향 적합성 색인이고 매수·매도 추천이 아닙니다.",
            "동일 signal_id의 원문 인용·근거 해시·촉매·무효화 조건은 앞의 호스트 검증 원문 신호 후보(JSON)가 기준입니다. 이 색인은 이를 반복하지 않습니다.",
            "quote_contexts의 before와 after는 해당 인용 앞뒤의 원문 조각이며 다른 출처의 문장과 합치지 마십시오.",
            "매수·비중확대·보유에 새 근거를 연결할 때 bullish, 매도·비중축소에는 bearish 신호가 필요합니다.",
            "MENTION_ONLY와 neutral은 새로운 포트폴리오 편입 근거가 될 수 없으며 단순 언급은 관심종목으로만 남깁니다.",
            "linked_signal_ids의 원문 URL·대상·방향이 결정과 일치해야 합니다. 분야의 긍정 논지를 다른 개별주에 자동 전용하지 마십시오.",
            "linked_signal_ids는 행동을 지지하는 근거입니다. 반대 근거·무효화 가능성은 key_risks와 change_reason에서 검토하고 보유·매수의 지지 연결에 섞지 마십시오. 기존 보유의 원문 연결은 유지할 수 있지만 다른 종목의 과거 ID를 복사하지 마십시오.",
            "linked_insight_ids는 이번 응답의 insights에 실제로 작성한 ID만 사용하십시오. 과거 상태의 ID만 복사하지 마십시오.",
            "산업·원자재 논지를 ETF로 해석한 판단은 AI 추론이며 메르의 해당 ETF 직접 보유·추천으로 표현하지 마십시오.",
            "원래 방향 표현과 인용 주변 문맥에서 조건·반대 시나리오·가격과 금리의 관계를 확인하십시오. compatible_actions만으로 수혜를 확정하지 마십시오.",
            "requires_direction_review=true는 방향 표기에 긍정·부정 단어가 함께 있다는 뜻이며 올바른 방향을 확정하지 않습니다. 그 신호만으로 신규 매수·비중확대 근거를 만들지 마십시오. 기존 보유·위험 감소를 자동으로 막는 규칙은 아닙니다.",
            "원문에 없는 무효화 조건은 UNKNOWN입니다. AI가 제안하는 관찰 조건과 메르의 발언을 구분하십시오.",
            "새 근거가 없는 기존 판단을 자동 매도하거나 비중을 임의 변경하지 마십시오.",
        ],
        "market_data_status": "NOT_PROVIDED_BY_THIS_INDEX",
        "investment_bridge_status": "REQUIRES_REVIEW",
        "source_posts": sources,
    }
