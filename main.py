"""메르AI 모델 포트폴리오 운영 진입점."""

from __future__ import annotations

import os
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from runtime_modes import (
    REBALANCE_SOURCE_WINDOW_DAYS,
    get_run_policy,
    should_rebalance,
)


RUN_MODE = os.environ.get("RUN_MODE", "scheduled").lower()
RUN_POLICY = get_run_policy(RUN_MODE)
OPERATING_OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
OUTPUT_DIR = OPERATING_OUTPUT_DIR


def _prepare_temp_output_dir(mode: str) -> Path:
    path = OPERATING_OUTPUT_DIR / mode
    path.mkdir(parents=True, exist_ok=True)
    for filename in (
        "portfolio_state.json",
        "model_portfolio_ledger.json",
        "performance_cache.json",
        "posts_db.json",
    ):
        source = OPERATING_OUTPUT_DIR / filename
        target = path / filename
        if source.exists():
            shutil.copy2(source, target)
    return path


if RUN_POLICY.upload_artifact:
    OUTPUT_DIR = _prepare_temp_output_dir(RUN_MODE)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["OUTPUT_DIR"] = str(OUTPUT_DIR)

if RUN_MODE == "test" and __name__ == "__main__":
    print("test 모드는 python -m unittest discover -s tests -v 로 실행합니다.")
    sys.exit(0)

from generate_dashboard import generate_all
from portfolio_output import build_markdown_report, build_output_model
from portfolio_provenance import enrich_decision_provenance, prepare_post_signal_events
from portfolio_runtime import (
    PortfolioPolicyBlocked,
    allocate_projected_state,
    security_key,
    validate_rebalance_coverage,
)
from portfolio_schema import (
    AnalysisDecisionV2,
    advance_watchlist_lifecycle,
    apply_analysis_decision,
    load_portfolio_state_file,
    parse_analysis_decision,
    parse_portfolio_state,
    save_analysis_decision_file,
    save_portfolio_state_file,
    validate_signal_ledger_append_only,
)
from run_bundle import commit_json_bundle, recover_pending_bundle
from telegram_notify import send_photo, send_status, send_structured_summary
from track_returns import (
    MODEL_LEDGER_FILE,
    apply_structured_transactions,
    get_structured_prices,
    load_model_ledger,
    refresh_structured_performance,
    refresh_structured_corporate_actions,
    record_model_snapshot,
    sanitize_model_ledger_for_state,
    sanitize_performance_cache_for_state,
    sanitize_performance_files_for_state,
    save_model_ledger,
    transaction_decisions_for_run,
)


STATE_PATH = OUTPUT_DIR / "portfolio_state.json"
DECISION_PATH = OUTPUT_DIR / "decision_latest.json"
CACHE_PATH = OUTPUT_DIR / "performance_cache.json"
PENDING_BUNDLE_PATH = OUTPUT_DIR / ".pending_run_bundle.json"
RUN_STATUS_PATH = OUTPUT_DIR / "run_status.json"
TRANSACTION_COST_BPS = {"KR": 30.0, "US": 35.0}
TELEGRAM_DELIVERY_FAILURE_EXIT_CODE = 2
RUN_STATUS_ENABLED = os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
_fetch_days_env = os.environ.get("FETCH_DAYS", "").strip()
FETCH_DAYS = int(_fetch_days_env) if _fetch_days_env else RUN_POLICY.fetch_days
KST = ZoneInfo("Asia/Seoul")


def analyze_posts_structured(*args, **kwargs):
    from analyze import analyze_posts_structured as run
    return run(*args, **kwargs)


def fetch_recent_posts(*args, **kwargs):
    from fetch_mer import fetch_recent_posts as run
    return run(*args, **kwargs)


def get_last_fetch_new_post_urls(*args, **kwargs):
    from fetch_mer import get_last_fetch_new_post_urls as run
    return run(*args, **kwargs)


def load_cached_posts(*args, **kwargs):
    from fetch_mer import load_cached_posts as run
    return run(*args, **kwargs)


def select_new_relevant_posts(*args, **kwargs):
    from fetch_mer import select_new_relevant_posts as run
    return run(*args, **kwargs)


def select_rebalance_posts(*args, **kwargs):
    from fetch_mer import select_rebalance_posts as run
    return run(*args, **kwargs)


def mark_posts_analysis_completed(*args, **kwargs):
    from fetch_mer import mark_posts_analysis_completed as run
    return run(*args, **kwargs)


def _to_kst_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST).replace(tzinfo=None)


def _now_kst() -> datetime:
    return _to_kst_naive(datetime.now(timezone.utc))


def _empty_state():
    return parse_portfolio_state({
        "schema_version": "2.1",
        "portfolio": [],
        "watchlist": [],
        "watchlist_archive": [],
        "closed_positions": [],
        "decision_history": [],
        "signal_events": [],
        "last_watchlist_changes": {
            "date": None,
            "added": [],
            "updated": [],
            "promoted": [],
            "rejected": [],
            "expired": [],
            "archived": [],
        },
        "insights": [],
        "last_rebalanced_date": None,
    })


def _load_state():
    recover_pending_bundle(PENDING_BUNDLE_PATH)
    if not STATE_PATH.exists():
        return _empty_state()
    return load_portfolio_state_file(STATE_PATH)


def _save_report(report: str, today: datetime) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"report_{today:%Y%m%d}.md"
    path.write_text(report, encoding="utf-8")
    return path


def _save_error_log(message: str, today: datetime) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"error_{today:%Y%m%d_%H%M%S}.log"
    path.write_text(message, encoding="utf-8")
    print(f"  오류 로그: {path}")


def _notify_status(title: str, body: str) -> None:
    if RUN_POLICY.send_telegram:
        send_status(title, body)


def _delivery_run_label(*, is_rebalance: bool = False) -> str:
    """Describe the actual operating path shown in a Telegram report."""
    if RUN_POLICY.mode == "scheduled" and is_rebalance:
        return "scheduled_rebalance"
    return RUN_POLICY.mode


def _defer_telegram_delivery_to_workflow() -> bool:
    """Keep a live report unsent until the workflow has persisted its state.

    A scheduled/rebalance run can otherwise notify the user about a decision
    that a later git push fails to preserve.  Verification modes intentionally
    remain direct, because they operate in isolated, non-persistent output.
    """
    requested = os.environ.get(
        "DEFER_TELEGRAM_DELIVERY_TO_WORKFLOW",
        "",
    ).strip().lower()
    return (
        requested in {"1", "true", "yes", "on"}
        and bool(RUN_POLICY.persist_operating_state)
    )


def _send_user_report(
    png_path: Path | None,
    state: dict,
    today: datetime,
    performance: dict,
    *,
    no_changes: bool = False,
    status_note: str = "",
    run_label: str | None = None,
) -> bool:
    """Deliver both Telegram artifacts or make the Action visibly fail.

    The chart and structured summary are the user-facing report.  Treating a
    failed chart as an invisible success made a green Action misleading, so
    both delivery receipts must be accepted before the run is considered
    complete.
    """
    photo_ok = False
    if png_path and png_path.exists():
        photo_ok = send_photo(
            str(png_path),
            f"모델 포트폴리오 성과 | {today:%Y년 %m월 %d일}",
        )
    else:
        print("  !! Telegram chart attachment missing; report delivery is incomplete")

    summary_ok = send_structured_summary(
        state,
        today.strftime("%Y년 %m월 %d일"),
        performance,
        no_changes=no_changes,
        status_note=status_note,
        include_dashboard_link=RUN_POLICY.persist_operating_state,
        run_label=run_label or _delivery_run_label(),
    )
    if photo_ok and summary_ok:
        return True

    failed = []
    if not photo_ok:
        failed.append("chart")
    if not summary_ok:
        failed.append("structured_summary")
    print("  !! Telegram report delivery incomplete: " + ", ".join(failed))
    return False


def _reset_run_status() -> None:
    """Remove a prior Actions receipt before starting a new run."""
    if not RUN_STATUS_ENABLED:
        return
    try:
        RUN_STATUS_PATH.unlink()
    except FileNotFoundError:
        pass


def _write_run_status(
    *,
    today: datetime,
    report_path: Path,
    chart_path: Path | None,
    no_changes: bool,
    run_label: str,
    delivery_required: bool,
    delivery_deferred: bool,
    delivery_accepted: bool | None,
    status_note: str,
) -> None:
    """Persist an Actions-only receipt after state and report generation complete.

    A notification outage must make the workflow red, but it must not discard
    an already validated portfolio state and force an inconsistent re-analysis
    tomorrow.  The workflow uses this receipt to distinguish that safe partial
    failure from a crash before the report was produced.
    """
    if not RUN_STATUS_ENABLED:
        return
    payload = {
        "schema_version": 1,
        "github_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "mode": RUN_MODE,
        "run_label": run_label,
        "completed_at": today.isoformat(),
        "state_bundle_committed": True,
        "report_path": report_path.name,
        "chart_path": chart_path.name if chart_path else None,
        "no_changes": bool(no_changes),
        "telegram_delivery_required": bool(delivery_required),
        "telegram_delivery_deferred": bool(delivery_deferred),
        "telegram_delivery_accepted": (
            bool(delivery_accepted)
            if isinstance(delivery_accepted, bool)
            else None
        ),
        "status_note": str(status_note or "")[:2_000],
    }
    RUN_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = RUN_STATUS_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(RUN_STATUS_PATH)


def _finalize_user_report(
    *,
    report_path: Path,
    png_path: Path | None,
    state: dict,
    today: datetime,
    performance: dict,
    no_changes: bool = False,
    status_note: str = "",
    run_label: str | None = None,
) -> int:
    """Deliver a generated report and record a safe Actions completion receipt."""
    effective_label = run_label or _delivery_run_label()
    delivery_required = bool(RUN_POLICY.send_telegram)
    delivery_deferred = (
        delivery_required and _defer_telegram_delivery_to_workflow()
    )
    delivery_accepted: bool | None = True
    if delivery_deferred:
        delivery_accepted = None
        print(
            "  Telegram delivery deferred until the GitHub Actions "
            "report/state push succeeds"
        )
    elif delivery_required:
        delivery_accepted = _send_user_report(
            png_path,
            state,
            today,
            performance,
            no_changes=no_changes,
            status_note=status_note,
            run_label=effective_label,
        )
    _write_run_status(
        today=today,
        report_path=report_path,
        chart_path=png_path,
        no_changes=no_changes,
        run_label=effective_label,
        delivery_required=delivery_required,
        delivery_deferred=delivery_deferred,
        delivery_accepted=delivery_accepted,
        status_note=status_note,
    )
    return (
        TELEGRAM_DELIVERY_FAILURE_EXIT_CODE
        if delivery_accepted is False
        else 0
    )


def _short_title(title: str, limit: int = 34) -> str:
    title = " ".join(str(title or "").split())
    if len(title) <= limit:
        return title
    return title[: limit - 1] + "…"


def _post_date_after(post: dict, cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(post["date"]) > cutoff
    except Exception:
        return False


def _posts_in_current_analysis_scope(
    posts: list[dict],
    *,
    is_rebalance: bool,
    state,
    today: datetime,
) -> list[dict]:
    if is_rebalance:
        try:
            cutoff = _rebalance_cutoff_date(state, today)
        except Exception:
            cutoff = today - timedelta(days=FETCH_DAYS)
        return [post for post in posts if _post_date_after(post, cutoff)]
    new_urls = get_last_fetch_new_post_urls()
    return [
        post
        for post in posts
        if post.get("url") in new_urls or post.get("analysis_status") == "pending"
    ]


def _rebalance_cutoff_date(state, today: datetime) -> datetime:
    if RUN_POLICY.mode == "rebalance" or state.last_rebalanced_date is None:
        # Keep the blocked-source scope identical to fetch_mer.select_rebalance_posts
        # when there is no prior full rebalance.  A scheduled first rebalance
        # otherwise falls back to its normal two-day fetch window and could make
        # a decision from only part of the two-week source window.  The cadence
        # (weekly) is intentionally independent from this evidence lookback.
        return today - timedelta(days=REBALANCE_SOURCE_WINDOW_DAYS)
    return datetime.fromisoformat(state.last_rebalanced_date)


def _current_summary_version() -> int:
    from fetch_mer import SUMMARY_VERSION
    return SUMMARY_VERSION


def _has_current_summary_version(post: dict) -> bool:
    """Only the current source-summary schema may authorize analysis input."""
    return post.get("summary_version") == _current_summary_version()


def _legacy_summary_upgrade_blocks_for_rebalance(
    posts: list[dict],
    *,
    is_rebalance: bool,
    state,
    today: datetime,
) -> list[dict]:
    """Fail closed while a bounded v2->v4 cache upgrade is incomplete.

    A rebalance uses the complete current source window.  Letting it consume
    just the few entries upgraded in this run would create a partial-history
    decision, so it waits until every in-scope cached summary has the current
    signal schema.  Regular new-post runs remain able to process genuinely new
    v4 summaries; legacy rows are filtered before analysis below.
    """
    if not is_rebalance:
        return []
    expected = _current_summary_version()
    blocked = []
    for post in _posts_in_current_analysis_scope(
        posts,
        is_rebalance=True,
        state=state,
        today=today,
    ):
        if _has_current_summary_version(post):
            continue
        blocked.append({
            "title": post.get("title", "제목 없음"),
            "url": post.get("url", ""),
            "date": post.get("date", ""),
            "reason": f"최신 출처 요약(v{expected}) 업그레이드 대기",
        })
    return blocked


def _summary_block_reason(post: dict) -> str:
    if post.get("summary_status") == "deferred":
        error = str(post.get("summary_error") or "").strip()
        return error or "글별 Flash 요약 실패"
    if not str(post.get("summary") or "").strip() and post.get("investment_relevant") is not False:
        return "글별 요약 없음"
    return ""


def _blocked_summary_posts_for_run(
    posts: list[dict],
    *,
    is_rebalance: bool,
    state,
    today: datetime,
) -> list[dict]:
    blocked = []
    for post in _posts_in_current_analysis_scope(
        posts,
        is_rebalance=is_rebalance,
        state=state,
        today=today,
    ):
        reason = _summary_block_reason(post)
        if not reason:
            continue
        blocked.append({
            "title": post.get("title", "제목 없음"),
            "url": post.get("url", ""),
            "date": post.get("date", ""),
            "reason": reason,
        })
    return blocked


def _deferred_status_note(posts: list[dict]) -> str:
    if not posts:
        return ""
    titles = ", ".join(_short_title(post.get("title", "")) for post in posts[:2])
    if len(posts) > 2:
        titles += f" 외 {len(posts) - 2}건"
    upgrade_waiting = sum(
        "업그레이드 대기" in str(post.get("reason") or "")
        for post in posts
    )
    if upgrade_waiting == len(posts):
        return (
            f"기존 출처 요약 {len(posts)}건 v4 업그레이드 진행으로 "
            f"전체 리밸런싱 보류: {titles}"
        )
    if upgrade_waiting:
        return (
            f"출처 요약 업그레이드 대기 {upgrade_waiting}건과 요약 보류 "
            f"{len(posts) - upgrade_waiting}건으로 투자 분석 보류: {titles}"
        )
    return f"새 글 {len(posts)}건 요약 실패/없음으로 투자 분석 보류: {titles}"


def _prices_for_ledger(ledger: dict) -> dict[str, float]:
    return get_structured_prices(ledger.get("positions", []))


def _run_no_change_update(
    state,
    today: datetime,
    status_note: str = "",
    deferred_posts: list[dict] | None = None,
    advance_lifecycle: bool = False,
    allow_maintenance: bool = False,
) -> int:
    if status_note:
        print(f"  {status_note}: 판단과 목표 비중을 유지하고 성과만 갱신합니다.")
    else:
        print("  신규 글 없음: 판단과 목표 비중을 유지하고 성과만 갱신합니다.")
    if advance_lifecycle:
        state = advance_watchlist_lifecycle(state, today.strftime("%Y-%m-%d"))
    ledger = load_model_ledger()
    ledger = sanitize_model_ledger_for_state(ledger, state.to_dict())
    if ledger.get("positions"):
        ledger = refresh_structured_corporate_actions(
            ledger,
            today.strftime("%Y-%m-%d"),
        )
    maintenance_changed = False
    if state.portfolio and allow_maintenance:
        pricing_items = list(state.portfolio) + ledger.get("positions", [])
        prices = get_structured_prices(pricing_items)
        if ledger.get("positions"):
            record_model_snapshot(
                ledger,
                prices,
                today.strftime("%Y-%m-%d"),
            )
        try:
            maintained_state, _ = allocate_projected_state(
                state,
                as_of_date=today.strftime("%Y-%m-%d"),
            )
            decisions = []
            for item in maintained_state.portfolio:
                row = dict(item)
                row["action"] = "보유"
                row["previous_weight"] = next(
                    (
                        float(current.get("proposed_weight") or 0.0)
                        for current in state.portfolio
                        if security_key(current) == security_key(item)
                    ),
                    0.0,
                )
                row["change_reason"] = (
                    str(row.get("change_reason") or "")
                    + " / 일일 실제비중·위험 정책 점검"
                ).strip(" /")
                decisions.append(row)
            before_tx_count = len(ledger.get("transactions", []))
            ledger = apply_structured_transactions(
                ledger,
                transaction_decisions_for_run(ledger, maintained_state.portfolio, decisions),
                prices,
                today.strftime("%Y-%m-%d"),
                cost_bps_by_market=TRANSACTION_COST_BPS,
            )
            maintenance_changed = (
                len(ledger.get("transactions", [])) > before_tx_count
                or maintained_state.to_dict() != state.to_dict()
            )
            state = maintained_state
        except PortfolioPolicyBlocked as exc:
            status_note = (
                (status_note + " / ") if status_note else ""
            ) + f"위험 allocator 보류: {exc}"
        cache = refresh_structured_performance(
            ledger,
            prices,
            today.strftime("%Y-%m-%d"),
            persist=False,
            fetch_benchmark=True,
        )
        cache = sanitize_performance_cache_for_state(cache, state.to_dict())
    elif ledger.get("positions"):
        prices = _prices_for_ledger(ledger)
        cache = refresh_structured_performance(
            ledger,
            prices,
            today.strftime("%Y-%m-%d"),
            persist=False,
            fetch_benchmark=True,
        )
        cache = sanitize_performance_cache_for_state(cache, state.to_dict())
    elif ledger.get("legacy_epochs"):
        cache = {
            "updated": today.strftime("%Y-%m-%d"),
            "epoch_id": ledger.get("epoch_id"),
            "inception_date": ledger.get("inception_date"),
            "legacy_epoch_count": len(ledger.get("legacy_epochs", []) or []),
            "portfolio_return_krw": 0.0,
            "cash": float(ledger.get("cash", 100.0)),
            "actual_cash_weight": 100.0,
            "realized_pnl": 0.0,
            "cumulative_costs": 0.0,
            "active_positions": [],
            "closed_positions": [],
            "report_summaries": [],
            "risk_metrics": {},
            "benchmark": {"status": "insufficient_history", "period_returns": []},
        }
    else:
        cache = sanitize_performance_files_for_state(state.to_dict(), persist=False)
    commit_json_bundle(
        {
            STATE_PATH: state.to_dict(),
            MODEL_LEDGER_FILE: ledger,
            CACHE_PATH: cache,
        },
        manifest_path=PENDING_BUNDLE_PATH,
    )
    output_state = state.to_dict()
    if status_note:
        output_state["status_note"] = status_note
    if deferred_posts:
        output_state["deferred_posts"] = deferred_posts
    output = build_output_model(
        output_state,
        cache,
        today_str=today.strftime("%Y-%m-%d"),
        status_note=status_note,
        approved_changes=False,
    )
    report = build_markdown_report(output)
    report_path = _save_report(report, today)
    _, png_path = generate_all(
        report,
        today,
        state=output_state,
        approved_changes=False,
    )
    return _finalize_user_report(
        report_path=report_path,
        png_path=png_path,
        state=output_state,
        today=today,
        performance=cache,
        no_changes=not maintenance_changed,
        status_note=status_note,
    )


def _is_llm_service_unavailable_error(exc: Exception) -> bool:
    message = str(exc)
    return "Gemini 투자 판단 보류" in message or "ChatGPT 투자 판단 보류" in message


def _is_pro_server_busy_investment_deferral(exc: Exception) -> bool:
    return "GEMINI_TRANSIENT" in str(exc)


def _gemini_deferral_note(exc: Exception) -> str:
    message = str(exc)
    if "ChatGPT 투자 판단 보류" in message:
        return "ChatGPT 인증·사용 한도 또는 판단 검증 문제로 투자 판단 보류. 포트폴리오 비중은 변경하지 않음"
    if "GEMINI_PERMANENT" in message:
        return "Gemini 모델 종료·권한 또는 요청 설정 오류로 투자 판단 보류. 포트폴리오 비중은 변경하지 않음"
    if "GEMINI_RATE_LIMIT" in message:
        return "Gemini 무료 API 한도 초과로 투자 판단 보류. 포트폴리오 비중은 변경하지 않음"
    if "GEMINI_TRANSIENT" in message:
        return "Gemini 일시 장애로 투자 판단 보류. 포트폴리오 비중은 변경하지 않음"
    return "Gemini 투자 판단 검증 실패로 판단 보류. 포트폴리오 비중은 변경하지 않음"


def _collect_posts() -> list[dict]:
    fetch_recent_posts(days=FETCH_DAYS)
    return load_cached_posts()


def _portfolio_identity(item: dict) -> tuple[str, str, str]:
    return (
        str(item.get("asset_type", "")).strip().lower(),
        str(item.get("market", "")).strip().upper(),
        str(item.get("code", "")).strip().upper(),
    )


def _exclude_unpriceable_new_suggestions(
    decision: AnalysisDecisionV2,
    state,
) -> AnalysisDecisionV2:
    """Keep existing holdings strict while dropping unverifiable new suggestions."""
    existing = {_portfolio_identity(item) for item in state.portfolio}
    accepted = []
    for item in decision.portfolio_decisions:
        try:
            get_structured_prices([item])
        except ValueError:
            if _portfolio_identity(item) in existing:
                raise
            print(
                "    가격 검증 불가 신규 포트폴리오 제안 제외: "
                + str(item.get("name") or "이름 없음")
            )
            continue
        accepted.append(item)
    return parse_analysis_decision({
        **decision.to_dict(),
        "portfolio_decisions": accepted,
    })


def _reject_unverified_new_positions(before_state, projected_state) -> None:
    existing = {security_key(item) for item in before_state.portfolio}
    unverified = [
        item
        for item in projected_state.portfolio
        if security_key(item) not in existing
        and item.get("provenance_status") != "verified"
    ]
    if unverified:
        names = ", ".join(str(item.get("name") or security_key(item)) for item in unverified)
        raise PortfolioPolicyBlocked(f"new positions lack verified source signals: {names}")


def _transaction_decisions_for_targets(
    before_state,
    target_state,
    analysis: AnalysisDecisionV2,
    *,
    full_rebalance: bool,
) -> list[dict]:
    before = {security_key(item): item for item in before_state.portfolio}
    changed = {security_key(item) for item in analysis.portfolio_decisions}
    rows: list[dict] = []
    for item in target_state.portfolio:
        key = security_key(item)
        if not full_rebalance and key not in changed and key in before:
            continue
        row = dict(item)
        previous_weight = float(before.get(key, {}).get("proposed_weight", 0.0) or 0.0)
        target_weight = float(row.get("proposed_weight", 0.0) or 0.0)
        row["previous_weight"] = previous_weight
        if key not in before:
            row["action"] = "매수"
        elif target_weight > previous_weight + 1e-9:
            row["action"] = "비중확대"
        elif target_weight < previous_weight - 1e-9:
            row["action"] = "비중축소"
        else:
            row["action"] = "보유"
        row["change_reason"] = (
            str(row.get("change_reason") or "")
            + " / 결정론적 슬리브·변동성·상한 정책 적용"
        ).strip(" /")
        rows.append(row)
    active_keys = {security_key(item) for item in target_state.portfolio}
    sell_keys: set[str] = set()
    for item in analysis.portfolio_decisions:
        if item.get("action") == "매도" and security_key(item) not in active_keys:
            rows.append(dict(item))
            sell_keys.add(security_key(item))
    for key, item in before.items():
        if key in active_keys or key in sell_keys:
            continue
        row = dict(item)
        row.update({
            "action": "매도",
            "previous_weight": float(item.get("proposed_weight") or 0.0),
            "proposed_weight": 0.0,
            "change_reason": str(
                item.get("policy_change_reason")
                or "결정론적 정책 목표 0%로 종료"
            ),
        })
        rows.append(row)
    return rows


def _decision_with_allocated_weights(
    analysis: AnalysisDecisionV2,
    target_state,
) -> AnalysisDecisionV2:
    targets = {security_key(item): item for item in target_state.portfolio}
    decisions = []
    for item in analysis.portfolio_decisions:
        row = dict(item)
        target = targets.get(security_key(row))
        if target is not None:
            row["proposed_weight"] = target["proposed_weight"]
            row["allocation_method"] = target.get("allocation_method")
            row["policy_action"] = target.get("policy_action")
            row["policy_change_reason"] = target.get("policy_change_reason")
        else:
            row["proposed_weight"] = 0.0
            was_held = float(row.get("previous_weight") or 0.0) > 0.0
            if row.get("action") == "매도" or was_held:
                row["allocation_method"] = "model_or_policy_exit"
                row["policy_action"] = "매도"
                row["policy_change_reason"] = "모델 판단 또는 결정론적 위험 정책으로 목표 0% 청산"
            else:
                row["allocation_method"] = "allocator_rejected_or_risk_blocked"
                row["policy_action"] = "편입 보류"
                row["policy_change_reason"] = "결정론적 배분·위험 정책에서 신규 목표 0%"
        decisions.append(row)
    return AnalysisDecisionV2(
        analysis_date=analysis.analysis_date,
        run_type=analysis.run_type,
        insights=analysis.insights,
        portfolio_decisions=decisions,
        watchlist=analysis.watchlist,
    )


def main() -> int:
    if RUN_MODE == "test":
        print("test 모드는 python -m unittest discover -s tests -v 로 실행합니다.")
        return 0

    today = _now_kst()
    today_date = today.strftime("%Y-%m-%d")
    print("=" * 60)
    print("  메르AI 모델 포트폴리오 실행")
    print(f"  모드: {RUN_MODE} | 출력: {OUTPUT_DIR}")
    print("=" * 60)

    try:
        _reset_run_status()
        state = _load_state()
        if RUN_MODE == "verify":
            return _run_no_change_update(
                state,
                today,
                status_note="검증 실행: 현재 포트폴리오 기준 출력만 확인(Gemini 분석 없음)",
            )

        is_rebalance = should_rebalance(
            RUN_MODE,
            state.last_rebalanced_date,
            today.date(),
        )
        cached_posts = _collect_posts()
        if is_rebalance:
            posts = select_rebalance_posts(
                cached_posts,
                None if RUN_POLICY.mode == "rebalance" else state.last_rebalanced_date,
                today,
            )
        else:
            posts = select_new_relevant_posts(
                cached_posts,
                get_last_fetch_new_post_urls(),
            )
        # A pre-v4 cached summary has no host-validated signal candidates.
        # It may be refreshed in the background, but it can never enter the
        # decision model as though it were a newly verified source.
        posts = [post for post in posts if _has_current_summary_version(post)]
        blocked_posts = _blocked_summary_posts_for_run(
            cached_posts,
            is_rebalance=is_rebalance,
            state=state,
            today=today,
        )
        legacy_upgrade_blocks = _legacy_summary_upgrade_blocks_for_rebalance(
            cached_posts,
            is_rebalance=is_rebalance,
            state=state,
            today=today,
        )
        if legacy_upgrade_blocks:
            existing_blocked_urls = {str(item.get("url") or "") for item in blocked_posts}
            blocked_posts.extend(
                item
                for item in legacy_upgrade_blocks
                if str(item.get("url") or "") not in existing_blocked_urls
            )
        deferred_note = _deferred_status_note(blocked_posts)
        if blocked_posts:
            print("  요약 보류 글이 남아 있어 이번 투자 판단 전체를 보류합니다.")
            return _run_no_change_update(
                state,
                today,
                status_note=deferred_note,
                deferred_posts=blocked_posts,
                # Expiring a stale observation is deterministic housekeeping,
                # not an investment decision.  Keep the list healthy even
                # while source analysis is fail-closed.
                advance_lifecycle=True,
                allow_maintenance=False,
            )
        if not posts:
            if is_rebalance:
                print("  리밸런싱 연기: 마지막 리밸런싱 이후 투자 관련 신규 글이 없습니다.")
            return _run_no_change_update(
                state,
                today,
                advance_lifecycle=True,
                allow_maintenance=True,
            )

        try:
            prepared_posts, source_signal_events = prepare_post_signal_events(
                posts,
                created_at=today.isoformat(),
                model_id=os.environ.get("GEMINI_SUMMARY_MODEL", "gemini-3.1-flash-lite"),
            )
            result = analyze_posts_structured(
                prepared_posts,
                today_date,
                state.to_dict(),
                is_rebalance=is_rebalance,
            )
        except RuntimeError as exc:
            if not _is_llm_service_unavailable_error(exc):
                raise
            note = _gemini_deferral_note(exc)
            if deferred_note:
                note += f" / {deferred_note}"
            print(f"  {note}: 기존 포트폴리오 상태로 출력만 갱신합니다.")
            _save_error_log(f"{type(exc).__name__}: {exc}", today)
            return _run_no_change_update(
                state,
                today,
                status_note=note,
                deferred_posts=blocked_posts,
                advance_lifecycle=True,
                allow_maintenance=False,
            )
        try:
            priceable_decision = _exclude_unpriceable_new_suggestions(
                result.decision,
                state,
            )
            validate_rebalance_coverage(state, priceable_decision)
            policy_state = state
            enriched_decision, signal_events = enrich_decision_provenance(
                priceable_decision,
                source_signal_events,
                created_at=today.isoformat(),
                model_id=getattr(
                    result,
                    "decision_model_version",
                    os.environ.get("GEMINI_DECISION_MODEL", "gemini-3.5-flash"),
                ),
            )
            projected_state = apply_analysis_decision(
                policy_state,
                enriched_decision,
                new_signal_events=signal_events,
            )
            _reject_unverified_new_positions(policy_state, projected_state)
            ledger = load_model_ledger()
            ledger = sanitize_model_ledger_for_state(ledger, projected_state.to_dict())
            if ledger.get("positions"):
                ledger = refresh_structured_corporate_actions(ledger, today_date)
            pretrade_prices = get_structured_prices(
                projected_state.portfolio + ledger.get("positions", [])
            )
            if ledger.get("positions"):
                record_model_snapshot(ledger, pretrade_prices, today_date)
            updated_state, allocation_summary = allocate_projected_state(
                projected_state,
                as_of_date=today_date,
            )
            enriched_decision = _decision_with_allocated_weights(
                enriched_decision,
                updated_state,
            )
            transaction_decisions = _transaction_decisions_for_targets(
                state,
                updated_state,
                enriched_decision,
                # The allocator recalculates every verified target even on a
                # regular signal run; execute the same full target set so state
                # and ledger can never diverge.
                full_rebalance=True,
            )
            transaction_decisions = transaction_decisions_for_run(
                ledger,
                updated_state.portfolio,
                transaction_decisions,
            )
            pricing_items = transaction_decisions + ledger.get("positions", [])
            prices = dict(pretrade_prices)
            missing_price_items = [
                item
                for item in pricing_items
                if security_key(item) not in prices
            ]
            if missing_price_items:
                prices.update(get_structured_prices(missing_price_items))
            ledger = apply_structured_transactions(
                ledger,
                transaction_decisions,
                prices,
                today_date,
                cost_bps_by_market=TRANSACTION_COST_BPS,
            )
            cache = refresh_structured_performance(
                ledger,
                prices,
                today_date,
                persist=False,
                fetch_benchmark=True,
            )
            cache = sanitize_performance_cache_for_state(cache, updated_state.to_dict())
        except (PortfolioPolicyBlocked, ValueError) as exc:
            note = f"포트폴리오 안전 검증으로 변경 보류: {exc}"
            _save_error_log(f"{type(exc).__name__}: {exc}", today)
            return _run_no_change_update(
                state,
                today,
                status_note=note,
                deferred_posts=blocked_posts,
                advance_lifecycle=True,
                allow_maintenance=False,
            )

        validate_signal_ledger_append_only(
            state.signal_events,
            updated_state.signal_events,
        )
        commit_json_bundle(
            {
                DECISION_PATH: enriched_decision.to_dict(),
                STATE_PATH: updated_state.to_dict(),
                MODEL_LEDGER_FILE: ledger,
                CACHE_PATH: cache,
            },
            manifest_path=PENDING_BUNDLE_PATH,
        )
        mark_posts_analysis_completed(
            {str(post.get("url") or "") for post in prepared_posts if post.get("url")},
            today_date,
        )
        output_state = updated_state.to_dict()
        output_state["allocation_summary"] = allocation_summary
        if deferred_note:
            output_state["status_note"] = deferred_note
        if blocked_posts:
            output_state["deferred_posts"] = blocked_posts
        output = build_output_model(
            output_state,
            cache,
            today_str=today_date,
            status_note=deferred_note,
        )
        report = build_markdown_report(output)
        report_path = _save_report(report, today)
        _, png_path = generate_all(report, today, state=output_state)

        result_code = _finalize_user_report(
            report_path=report_path,
            png_path=png_path,
            state=output_state,
            today=today,
            performance=cache,
            status_note=deferred_note,
            run_label=_delivery_run_label(
                is_rebalance=enriched_decision.run_type == "rebalance",
            ),
        )
        if result_code == 0:
            print(f"  완료: 포트폴리오 {len(updated_state.portfolio)}종목")
        return result_code
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(f"  실행 실패: {message}")
        _save_error_log(message, today)
        _notify_status("MerAI run failed", message[:1500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
