"""Isolated provider comparison using cached sources and an immutable state snapshot.

The default command is an offline preview.  Live generation requires --live;
neither path imports main.py, fetches posts, regenerates summaries, sends Telegram,
or persists portfolio decisions to output/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from decision_providers import GenerationRequestLimit
from portfolio_provenance import enrich_decision_provenance, prepare_post_signal_events
from portfolio_runtime import allocate_projected_state, security_key, validate_rebalance_coverage
from portfolio_schema import apply_analysis_decision, parse_portfolio_state
from runtime_modes import REBALANCE_SOURCE_WINDOW_DAYS


REPOSITORY_ROOT = Path(__file__).resolve().parent
CURRENT_SUMMARY_VERSION = 4


class RequestBudgetExceeded(GenerationRequestLimit):
    """Raised before a generation request when its comparison budget is exhausted."""


class RequestBudget:
    """One shared, thread-safe generation-attempt budget, including retries.

    Providers call consume immediately before each actual generation transport.
    A failed response still consumes an attempt. Token counting is a separate
    provider operation and is not included in this generation-request counter.
    """

    def __init__(self, maximum: int):
        if maximum < 0:
            raise ValueError("maximum requests must be non-negative")
        self.maximum = maximum
        self._attempts: list[dict[str, Any]] = []
        self._response_metrics: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def consume(self, provider: str, model: str) -> None:
        with self._lock:
            if len(self._attempts) >= self.maximum:
                # Existing Gemini retry classification matches "exhausted"/
                # "quota"/"limit" as provider errors. This local stop must not
                # be mistaken for a provider 429 and retried with backoff.
                raise RequestBudgetExceeded("comparison generation request allowance spent")
            self._attempts.append({
                "attempt": len(self._attempts) + 1,
                "provider": provider,
                "model": model,
            })

    def record_response(self, metric: dict[str, Any]) -> None:
        """Retain usage even when a successful response later fails validation."""
        allowed = (
            "provider", "model", "input_tokens", "output_tokens", "thinking_tokens",
            "total_tokens", "latency_seconds", "status",
        )
        with self._lock:
            recorded = {key: metric.get(key) for key in allowed}
            recorded["status"] = recorded.get("status") or "completed"
            self._response_metrics.append(recorded)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "maximum": self.maximum,
                "generation_attempts": len(self._attempts),
                "remaining": self.maximum - len(self._attempts),
                "attempts": deepcopy(self._attempts),
                "responses": deepcopy(self._response_metrics),
            }


@dataclass(frozen=True)
class Variant:
    name: str
    provider: str
    context_mode: str


VARIANTS = {
    "gemini-baseline": Variant("gemini-baseline", "gemini", "baseline"),
    "gemini-focused": Variant("gemini-focused", "gemini", "focused"),
    "chatgpt-focused": Variant("chatgpt-focused", "chatgpt", "focused"),
}


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def snapshot_hashes(directory: Path) -> dict[str, str]:
    """Hash every existing operating file and detect additions/deletions too."""
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*")) if path.is_file()
    }


def _normalized_quote(text: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = re.sub(r"[\u200b-\u200f\u2028\u2029\ufeff\u00ad]", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def audit_source_quotes(posts: list[dict[str, Any]]) -> dict[str, Any]:
    """Repeat the source-summary quote check before trusting cached candidates."""
    checked = 0
    failures: list[dict[str, Any]] = []
    for post in posts:
        source = _normalized_quote(post.get("content"))
        for index, candidate in enumerate(post.get("signal_candidates", []) or []):
            checked += 1
            quote = _normalized_quote(candidate.get("exact_text"))
            if not quote or quote not in source:
                failures.append({"post_url": post.get("url", ""), "candidate_index": index})
    return {"checked": checked, "matched": checked - len(failures), "failures": failures}


def _post_date(post: dict[str, Any]) -> date:
    return date.fromisoformat(str(post.get("date") or post.get("published_date") or "")[:10])


def _state_observation_dates(value: Any):
    """Include source/creation dates without treating future expiry as hindsight."""
    observed_date_fields = {
        "last_rebalanced_date", "analysis_date", "decision_date", "closed_date",
        "published_date", "created_at", "latest_evidence_date", "watchlist_entry_date",
        "portfolio_entry_date", "watchlist_closed_date",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if key in observed_date_fields and isinstance(item, str) and item:
                yield date.fromisoformat(item[:10])
            yield from _state_observation_dates(item)
    elif isinstance(value, list):
        for item in value:
            yield from _state_observation_dates(item)


def select_cached_posts(
    cached_posts: list[dict[str, Any]],
    state: dict[str, Any],
    analysis_date: str,
    run_type: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Mirror cached regular/rebalance scope without collection or acknowledgments."""
    as_of = date.fromisoformat(analysis_date)
    if run_type not in {"regular", "rebalance"}:
        raise ValueError("run_type must be regular or rebalance")
    # A present-day state cannot be used for an earlier historical decision.
    if any(observed > as_of for observed in _state_observation_dates(state)):
        raise ValueError("analysis date precedes the supplied state snapshot; use a historical snapshot")

    eligible_by_date = [post for post in cached_posts if _post_date(post) <= as_of]
    excluded_future = len(cached_posts) - len(eligible_by_date)
    cutoff = None
    if run_type == "rebalance":
        last_rebalanced = state.get("last_rebalanced_date")
        cutoff = date.fromisoformat(last_rebalanced) if last_rebalanced else as_of - timedelta(days=REBALANCE_SOURCE_WINDOW_DAYS)
        scoped = [post for post in eligible_by_date if _post_date(post) > cutoff]
    else:
        # There are no newly scraped URLs in an isolated cached comparison.
        scoped = [post for post in eligible_by_date if post.get("analysis_status") == "pending"]

    blocked = []
    selected = []
    for post in scoped:
        reason = ""
        if post.get("summary_status") == "deferred":
            reason = "cached summary deferred"
        elif not str(post.get("summary") or "").strip() and post.get("investment_relevant") is not False:
            reason = "cached summary missing"
        elif run_type == "rebalance" and post.get("summary_version") != CURRENT_SUMMARY_VERSION:
            reason = "cached source schema upgrade incomplete"
        if reason:
            blocked.append({"url": post.get("url", ""), "reason": reason})
        if (
            post.get("summary_version") == CURRENT_SUMMARY_VERSION
            and post.get("investment_relevant") is True
            and post.get("summary_status") != "deferred"
            and str(post.get("summary") or "").strip()
        ):
            selected.append(deepcopy(post))
    return selected, {
        "run_type": run_type,
        "cutoff_exclusive": cutoff.isoformat() if cutoff else None,
        "cached_posts": len(cached_posts),
        "scoped_posts": len(scoped),
        "selected_posts": len(selected),
        "future_posts_excluded": excluded_future,
        "blocked_posts": blocked,
    }


def _redacted_error(exc: Exception) -> str:
    text = str(exc)
    text = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?:sk-[A-Za-z0-9_\-]{8,}|AIza[A-Za-z0-9_\-]{15,})", "[REDACTED]", text)
    text = re.sub(r"(?i)((?:access_token|refresh_token|api_key|client_secret)[\"']?\s*[:=]\s*[\"']?)[^\s,\"'}]+", r"\1[REDACTED]", text)
    return f"{type(exc).__name__}: {text[:1500]}"


def _validate_and_project(decision: Any, state: dict[str, Any], events: list[dict[str, Any]], analysis_date: str, model: str):
    parsed_state = parse_portfolio_state(deepcopy(state))
    validate_rebalance_coverage(parsed_state, decision)
    enriched, source_events = enrich_decision_provenance(
        decision, deepcopy(events), created_at=analysis_date, model_id=model,
    )
    projected = apply_analysis_decision(parsed_state, enriched, new_signal_events=source_events)
    existing = {security_key(item) for item in parsed_state.portfolio}
    unverified = [
        item.get("name") or security_key(item)
        for item in projected.portfolio
        if security_key(item) not in existing and item.get("provenance_status") != "verified"
    ]
    if unverified:
        raise ValueError("new positions lack verified directional source signals: " + ", ".join(unverified))
    allocated, allocation = allocate_projected_state(projected, as_of_date=analysis_date)
    return enriched, allocated.to_dict(), allocation


def _portfolio_differences(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    previous = {security_key(item): item for item in baseline.get("portfolio", [])}
    current = {security_key(item): item for item in candidate.get("portfolio", [])}
    changes = []
    for key in sorted(previous.keys() | current.keys()):
        old, new = previous.get(key, {}), current.get(key, {})
        old_weight, new_weight = old.get("proposed_weight", 0), new.get("proposed_weight", 0)
        if key not in previous or key not in current or old_weight != new_weight or old.get("name") != new.get("name"):
            changes.append({
                "security": key, "name": new.get("name", old.get("name", "")),
                "baseline_weight": old_weight, "candidate_weight": new_weight,
                "weight_change_pp": round(float(new_weight) - float(old_weight), 10),
                "baseline_name": old.get("name"), "candidate_name": new.get("name"),
            })
    return changes


def _preview_input(posts, analysis_date, state, run_type, context_mode):
    import analyze
    # This helper constructs text only. It must never count tokens with a live SDK.
    return analyze.preview_decision_input(
        posts, analysis_date, state,
        is_rebalance=run_type == "rebalance", context_mode=context_mode,
    )


def _template_metadata() -> dict[str, Any]:
    import analyze
    from system_prompt import DECISION_SYSTEM_PROMPT
    filenames = (
        "analyze.py", "decision_context.py", "system_prompt.py",
        "portfolio_schema.py", "portfolio_provenance.py",
    )
    return {
        "decision_schema_sha256": _canonical_hash(analyze.DECISION_RESPONSE_SCHEMA),
        "system_prompt_sha256": hashlib.sha256(DECISION_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "code_file_sha256": {
            name: hashlib.sha256((REPOSITORY_ROOT / name).read_bytes()).hexdigest()
            for name in filenames if (REPOSITORY_ROOT / name).is_file()
        },
    }


def run_comparison(
    *,
    source_dir: Path,
    destination: Path,
    analysis_date: str,
    run_type: str = "regular",
    live: bool = False,
    variants: list[str] | None = None,
    repetitions: int = 1,
    max_requests: int = 3,
    gemini_model: str | None = None,
    chatgpt_model: str | None = None,
    analyze_fn: Callable | None = None,
    preview_fn: Callable | None = None,
) -> dict[str, Any]:
    """Compare copies of one input snapshot and save only experiment artifacts."""
    source_dir, destination = source_dir.resolve(), destination.resolve()
    repository_output = (REPOSITORY_ROOT / "output").resolve()
    protected = {source_dir, repository_output}
    if any(destination == path or path in destination.parents for path in protected):
        raise ValueError("comparison destination must be outside all operating output directories")
    if repetitions < 1:
        raise ValueError("repetitions must be at least one")
    variant_names = list(variants or VARIANTS)
    if not variant_names or any(name not in VARIANTS for name in variant_names):
        raise ValueError("unknown comparison variant")
    if len(set(variant_names)) != len(variant_names):
        raise ValueError("duplicate comparison variants")

    before = snapshot_hashes(source_dir)
    operating_before = snapshot_hashes(repository_output) if repository_output != source_dir else before
    cached = json.loads((source_dir / "posts_db.json").read_text(encoding="utf-8"))
    state = json.loads((source_dir / "portfolio_state.json").read_text(encoding="utf-8"))
    if not isinstance(cached, list) or not all(isinstance(post, dict) for post in cached):
        raise ValueError("posts_db.json must contain a list of post objects")
    selected, selection = select_cached_posts(cached, state, analysis_date, run_type)
    quotes = audit_source_quotes(selected)
    prepared, source_events = prepare_post_signal_events(
        selected, created_at=analysis_date, model_id="cached-summary-unchanged",
    )
    input_hash = _canonical_hash({"posts": prepared, "state": state, "analysis_date": analysis_date, "run_type": run_type})
    budget = RequestBudget(max_requests)
    report = {
        "schema_version": "1.0", "mode": "live" if live else "dry-run",
        "analysis_date": analysis_date, "selection": selection,
        "input_sha256": input_hash, "state_sha256": _canonical_hash(state),
        "templates": _template_metadata(),
        "source_file_sha256_before": before,
        "operating_file_sha256_before": operating_before,
        "source_quote_audit": quotes, "prepared_signal_events": len(source_events),
        "repetitions": repetitions, "attempts": [],
        "limits": {
            "returns_verified": False,
            "market_ticker_validation": "not performed; no market-data requests",
            "character_counts_are_tokens": False,
            "historical_backtest": False,
            "raw_model_responses_saved": False,
        },
    }
    preview_fn = preview_fn or _preview_input
    if live and analyze_fn is None:
        from analyze import analyze_posts_structured
        analyze_fn = analyze_posts_structured
    baseline_projected = None
    try:
        for repetition in range(1, repetitions + 1):
            for name in variant_names:
                variant = VARIANTS[name]
                model = gemini_model if variant.provider == "gemini" else chatgpt_model
                record = {
                    "variant": name, "provider": variant.provider, "model_override": model,
                    "context_mode": variant.context_mode, "repetition": repetition,
                    "input_sha256": input_hash,
                }
                report["attempts"].append(record)
                if selection["blocked_posts"] or quotes["failures"] or not prepared:
                    record.update(status="blocked-input", error="cached input incomplete, invalid quotes, or no pending eligible posts")
                    continue
                try:
                    preview = preview_fn(deepcopy(prepared), analysis_date, deepcopy(state), run_type, variant.context_mode)
                except Exception as exc:
                    record.update(status="rejected-preview", error=_redacted_error(exc))
                    continue
                record.update(
                    request_preview_sha256=hashlib.sha256(preview.encode("utf-8")).hexdigest(),
                    request_preview_characters=len(preview),
                    context_count_stage="user-message only; before provider-specific schema instructions, fitting and token counting",
                )
                if not live:
                    record["status"] = "dry-run"
                    continue
                if not budget.summary()["remaining"]:
                    record.update(status="skipped-budget", error="generation-request budget exhausted")
                    continue
                started = time.monotonic()
                used_before = budget.summary()["generation_attempts"]
                metrics_before = len(budget.summary()["responses"])
                try:
                    result = analyze_fn(
                        deepcopy(prepared), analysis_date, deepcopy(state),
                        is_rebalance=run_type == "rebalance",
                        decision_provider=variant.provider, decision_model=model,
                        context_mode=variant.context_mode, request_budget=budget,
                        decision_validator=lambda decision: _validate_and_project(
                            decision, state, source_events, analysis_date, model or variant.provider,
                        ),
                    )
                    enriched, projected, allocation = _validate_and_project(
                        result.decision, state, source_events, analysis_date, result.decision_model_version,
                    )
                    record.update(
                        status="valid", decision_model_version=result.decision_model_version,
                        decision=enriched.to_dict(), allocation=allocation,
                        changes_vs_operating_state=_portfolio_differences(state, projected),
                    )
                    if name == "gemini-baseline" and baseline_projected is None:
                        baseline_projected = deepcopy(projected)
                    record["changes_vs_first_valid_gemini_baseline"] = (
                        _portfolio_differences(baseline_projected, projected)
                        if baseline_projected is not None else None
                    )
                    # Providers may expose aggregate usage. Absence is unknown, not zero.
                    request_metrics = getattr(result, "request_metrics", None)
                    record["request_metrics"] = (
                        [{key: metric.get(key) for key in (
                            "provider", "model", "input_tokens", "output_tokens",
                            "thinking_tokens", "total_tokens", "latency_seconds", "status",
                        )} for metric in request_metrics if isinstance(metric, dict)]
                        if isinstance(request_metrics, list) else None
                    )
                    record["successful_generation_responses"] = (
                        sum(metric.get("status") in {None, "completed"} for metric in record["request_metrics"])
                        if record["request_metrics"] is not None else None
                    )
                except Exception as exc:
                    record.update(status="rejected", error=_redacted_error(exc))
                finally:
                    retained_metrics = budget.summary()["responses"][metrics_before:]
                    if retained_metrics:
                        record["request_metrics"] = retained_metrics
                        record["successful_generation_responses"] = sum(metric["status"] == "completed" for metric in retained_metrics)
                    else:
                        record.setdefault("request_metrics", None)
                        record.setdefault("successful_generation_responses", None)
                    record["generation_attempts"] = budget.summary()["generation_attempts"] - used_before
                    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    finally:
        after = snapshot_hashes(source_dir)
        changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
        operating_after = snapshot_hashes(repository_output) if repository_output != source_dir else after
        if repository_output != source_dir:
            changed.extend(
                "repository_output/" + key
                for key in sorted(operating_before.keys() | operating_after.keys())
                if operating_before.get(key) != operating_after.get(key)
            )
        report.update(
            request_budget=budget.summary(), source_file_sha256_after=after,
            operating_file_sha256_after=operating_after,
            operating_state_unchanged=not changed, modified_operating_files=changed,
        )
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="offline preview; the default")
    mode.add_argument("--live", action="store_true", help="explicitly allow model generation requests")
    parser.add_argument("--source-dir", type=Path, default=REPOSITORY_ROOT / "output")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--analysis-date", default=datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat())
    parser.add_argument("--run-type", choices=("regular", "rebalance"), default="regular")
    parser.add_argument("--variants", nargs="+", choices=tuple(VARIANTS), default=list(VARIANTS))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-requests", type=int, default=3, help="total generation attempts, including retries and correction")
    parser.add_argument("--gemini-model")
    parser.add_argument("--chatgpt-model")
    args = parser.parse_args(argv)
    destination = args.output_dir or REPOSITORY_ROOT / "experiments" / ("comparison-" + datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S-%f"))
    report = run_comparison(
        source_dir=args.source_dir, destination=destination,
        analysis_date=args.analysis_date, run_type=args.run_type,
        live=args.live, variants=args.variants, repetitions=args.repetitions,
        max_requests=args.max_requests, gemini_model=args.gemini_model, chatgpt_model=args.chatgpt_model,
    )
    print(json.dumps({
        "report": str(destination / "comparison.json"), "mode": report["mode"],
        "selected_posts": report["selection"]["selected_posts"],
        "generation_attempts": report["request_budget"]["generation_attempts"],
        "operating_state_unchanged": report["operating_state_unchanged"],
        "statuses": [item["status"] for item in report["attempts"]],
    }, ensure_ascii=False))
    if not report["operating_state_unchanged"]:
        return 2
    return 0 if all(item["status"] in {"dry-run", "valid"} for item in report["attempts"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
