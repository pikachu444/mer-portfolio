"""
analyze.py
Gemini 기본 경로와 선택적 ChatGPT 구독 로그인으로 메르AI 판단을 생성한다.

투자 판단은 무료 티어의 안정 모델인 gemini-3.5-flash를 사용한다.
글별 요약 모델은 fetch_mer.py에서 별도로 설정한다.

API 키 발급: https://aistudio.google.com/app/apikey
환경변수: GEMINI_API_KEY
"""

import json
import os
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Dict, Tuple

from google import genai
from google.genai import types

from portfolio_schema import (
    AnalysisDecisionV2,
    apply_analysis_decision,
    parse_analysis_decision,
    parse_portfolio_state,
)
from system_prompt import (
    DECISION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_decision_user_message,
    build_user_message,
)
from gemini_utils import (
    DEFAULT_HTTP_TIMEOUT_MS,
    RETRY_BUDGET_SECONDS,
    generate_content_with_retry,
    is_daily_quota_error,
)
from decision_providers import (
    TrackedGeminiClient,
    consume_request,
    record_metric,
    resolve_context_mode,
    resolve_model,
    resolve_provider,
)


# ─── 모델 설정 ────────────────────────────────────────────────────────────────
#
# 역할별 모델을 명시해 모델 이름에 따른 암묵적 라우팅을 금지한다.
DECISION_MODEL = os.environ.get(
    "GEMINI_DECISION_MODEL",
    "gemini-3.5-flash",
).strip()
DECISION_MAX_ATTEMPTS = int(os.environ.get("GEMINI_DECISION_MAX_ATTEMPTS", "3"))
MODEL_INPUT_TOKEN_LIMIT = 65_536
MODEL_INPUT_SAFE_RATIO = 1.0
DECISION_OUTPUT_TOKEN_LIMIT = 24_576

_EVIDENCE_POST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "url": {"type": "string"},
        "published_date": {"type": "string", "format": "date"},
    },
    "required": ["title", "url", "published_date"],
}

_INSIGHT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "investment_implication": {"type": "string"},
        "evidence_posts": {"type": "array", "items": _EVIDENCE_POST_SCHEMA},
        "related_decision_codes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "id",
        "title",
        "summary",
        "investment_implication",
        "evidence_posts",
        "related_decision_codes",
    ],
}

_PORTFOLIO_DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "code": {"type": "string"},
        "market": {"type": "string"},
        "asset_type": {"type": "string", "enum": ["stock", "etf"]},
        "decision_actor": {"type": "string", "enum": ["메르", "AI"]},
        "action": {
            "type": "string",
            "enum": ["매수", "보유", "비중확대", "비중축소", "매도"],
        },
        "basis": {
            "type": "string",
            "enum": ["직접 발언", "종목 분석", "섹터 분석", "이전 판단 유지"],
        },
        "decision_date": {"type": "string", "format": "date"},
        "evidence_posts": {"type": "array", "items": _EVIDENCE_POST_SCHEMA},
        "source_mentioned": {"type": "boolean"},
        "previous_weight": {
            "anyOf": [{"type": "number"}, {"type": "null"}],
        },
        "proposed_weight": {"type": "number", "minimum": 0, "maximum": 100},
        "weight_source": {
            "type": "string",
            "enum": ["메르 직접 발언 기반", "AI 제안"],
        },
        "change_reason": {"type": "string"},
        "allocation_role": {
            "type": "string",
            "enum": ["core", "satellite", "risk", "defensive", "watch"],
        },
        "source_scope": {
            "type": "string",
            "enum": [
                "blogger_trade_disclosure",
                "source_named_security",
                "sector_only",
                "previous_decision",
            ],
        },
        "investment_rationale": {"type": "string"},
        "current_entry_reason": {"type": "string"},
        "key_risks": {"type": "array", "items": {"type": "string"}},
        "linked_insight_ids": {"type": "array", "items": {"type": "string"}},
        "linked_signal_ids": {"type": "array", "items": {"type": "string"}},
        "thesis_id": {"type": "string"},
        "issuer_id": {"type": "string"},
        "theme_ids": {"type": "array", "items": {"type": "string"}},
        "country_code": {"type": "string"},
        "quality_components": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "explicitness": {"type": "number", "minimum": 0, "maximum": 1},
                "causality": {"type": "number", "minimum": 0, "maximum": 1},
                "catalyst": {"type": "number", "minimum": 0, "maximum": 1},
                "confirmation": {"type": "number", "minimum": 0, "maximum": 1},
                "invalidation": {"type": "number", "minimum": 0, "maximum": 1},
                "recency": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": [
                "explicitness", "causality", "catalyst", "confirmation",
                "invalidation", "recency",
            ],
        },
    },
    "required": [
        "name",
        "code",
        "market",
        "asset_type",
        "decision_actor",
        "action",
        "basis",
        "decision_date",
        "evidence_posts",
        "source_mentioned",
        "previous_weight",
        "proposed_weight",
        "weight_source",
        "change_reason",
        "allocation_role",
        "source_scope",
        "investment_rationale",
        "current_entry_reason",
        "key_risks",
        "linked_insight_ids",
        "linked_signal_ids",
        "thesis_id",
        "issuer_id",
        "theme_ids",
        "country_code",
        "quality_components",
    ],
}

_WATCHLIST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "code": {"type": "string"},
        "market": {"type": "string"},
        "asset_type": {"type": "string", "enum": ["stock", "etf", "sector"]},
        "decision_actor": {"type": "string", "enum": ["메르", "AI"]},
        "basis": {
            "type": "string",
            "enum": ["직접 발언", "종목 분석", "섹터 분석", "이전 판단 유지"],
        },
        "decision_date": {"type": "string", "format": "date"},
        "evidence_posts": {"type": "array", "items": _EVIDENCE_POST_SCHEMA},
        "source_mentioned": {"type": "boolean"},
        "watchlist_entry_date": {"type": "string", "format": "date"},
        "latest_evidence_date": {"type": "string", "format": "date"},
        "watchlist_duration_days": {"type": "integer", "minimum": 0},
        "portfolio_entry_date": {
            "anyOf": [{"type": "string", "format": "date"}, {"type": "null"}],
        },
        "watchlist_closed_date": {
            "anyOf": [{"type": "string", "format": "date"}, {"type": "null"}],
        },
        "status": {
            "type": "string",
            "enum": ["관심", "재검토 필요", "포트폴리오 편입", "종료"],
        },
        "source_scope": {
            "type": "string",
            "enum": [
                "blogger_trade_disclosure",
                "source_named_security",
                "sector_only",
                "previous_decision",
            ],
        },
        "observation_reason": {"type": "string"},
        "linked_signal_ids": {"type": "array", "items": {"type": "string"}},
        "thesis_id": {"type": "string"},
        "watchlist_kind": {
            "type": "string",
            "enum": ["mention", "event", "cyclical", "structural"],
        },
    },
    "required": [
        "name",
        "code",
        "market",
        "asset_type",
        "decision_actor",
        "basis",
        "decision_date",
        "evidence_posts",
        "source_mentioned",
        "watchlist_entry_date",
        "latest_evidence_date",
        "watchlist_duration_days",
        "portfolio_entry_date",
        "watchlist_closed_date",
        "status",
        "source_scope",
        "observation_reason",
        "linked_signal_ids",
        "thesis_id",
        "watchlist_kind",
    ],
}

DECISION_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "analysis_date": {"type": "string", "format": "date"},
        "run_type": {"type": "string", "enum": ["regular", "rebalance"]},
        "insights": {"type": "array", "items": _INSIGHT_SCHEMA},
        "portfolio_decisions": {
            "type": "array",
            "items": _PORTFOLIO_DECISION_SCHEMA,
        },
        "watchlist": {"type": "array", "items": _WATCHLIST_SCHEMA},
    },
    "required": [
        "analysis_date",
        "run_type",
        "insights",
        "portfolio_decisions",
        "watchlist",
    ],
}

# 투자 분석 특성상 안전 필터 완화 (주식 분석 용어 오탐 방지)
SAFETY_SETTINGS = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
]


@dataclass(frozen=True)
class StructuredAnalysisResult:
    decision: AnalysisDecisionV2
    report: str
    decision_model_version: str
    decision_provider: str = "gemini"
    request_metrics: list[dict] = field(default_factory=list)


_MODEL_VERSION_BY_ID: dict[str, str] = {}


# ─── API 클라이언트 초기화 ────────────────────────────────────────────────────

def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY 환경변수가 설정되지 않았습니다.\n"
            "발급 방법: https://aistudio.google.com/app/apikey\n"
            "설정 방법: export GEMINI_API_KEY='your-key-here'"
        )
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=DEFAULT_HTTP_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


# ─── API 호출 재시도 헬퍼 ────────────────────────────────────────────────────

def call_gemini_with_retry(
    client: genai.Client,
    model: str,
    contents,
    config,
    max_retries: int = 3,
    *,
    retry_budget_seconds: float | None = None,
):
    """
    Gemini API 호출 시 rate limit 계열 오류가 발생하면 대기 후 재시도한다.
    """
    return generate_content_with_retry(
        client=client,
        model=model,
        contents=contents,
        config=config,
        max_retries=max_retries,
        http_timeout_ms=DEFAULT_HTTP_TIMEOUT_MS,
        retry_budget_seconds=retry_budget_seconds,
    )


# ─── 모델별 분석 시도 ─────────────────────────────────────────────────────────

def _model_sequence() -> list[str]:
    """Legacy Markdown analysis also uses the explicit decision model only."""
    return [DECISION_MODEL]


def _retry_count_for_model(model_name: str) -> int:
    return DECISION_MAX_ATTEMPTS


def _decision_model() -> str:
    return DECISION_MODEL


def _try_model(
    client: genai.Client,
    model_name: str,
    user_message: str,
    max_retries: int | None = None,
) -> Tuple[bool, str]:
    """
    특정 모델로 분석 시도.
    Returns: (성공 여부, 결과 텍스트 or 오류 메시지)
    """
    try:
        print(f"  모델 시도: {model_name}")

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=DECISION_OUTPUT_TOKEN_LIMIT,
            safety_settings=SAFETY_SETTINGS,
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.MEDIUM,
            ),
        )

        # Rate limit 재시도 헬퍼를 경유하여 API 호출
        response = call_gemini_with_retry(
            client=client,
            model=model_name,
            contents=user_message,
            config=config,
            max_retries=max_retries if max_retries is not None else _retry_count_for_model(model_name),
        )

        text = response.text
        
        # [강화된 응답 검증]
        if not text:
            return False, "응답이 완전히 비어 있음"
            
        # 1. 최소 길이 검증
        if len(text) < 1500:
            return False, f"응답 길이가 너무 짧아 분석 중단으로 의심됨 ({len(text)}자)"
            
        # 2. 필수 마크다운 세션 검증 (정규식 파싱 안전성 보증)
        required_headers = ["포트폴리오 추천"]
        missing_headers = [h for h in required_headers if h not in text]
        if missing_headers:
            return False, f"필수 세션 누락: {', '.join(missing_headers)} (Gemini 답변 중간 끊김 발생)"

        return True, text

    except Exception as e:
        err = str(e)

        # 재시도 후에도 남은 일시 오류 또는 할당량 초과
        if any(x in err.lower() for x in ("429", "quota", "rate", "resource", "503", "unavailable", "high demand")):
            print(f"    Gemini API 일시 오류 또는 quota 초과: {model_name} — {err[:180]}")
            return False, f"Gemini API 오류: {err}"
        # 모델 없음
        if "404" in err or "not found" in err.lower() or "invalid" in err.lower():
            print(f"    모델 없음/유효하지 않음: {model_name}")
            return False, f"모델 없음: {err}"
        # 기타 오류
        import traceback
        print(f"    ❌ API 예외 발생: {type(e).__name__} — {err[:120]}")
        print("    [상세 에러 트레이스백]")
        traceback.print_exc()
        return False, err


def _call_model_text(
    client: genai.Client,
    model_name: str,
    user_message: str,
    system_instruction: str,
    response_mime_type: str | None = None,
    max_retries: int | None = None,
    response_json_schema: dict | None = None,
    retry_budget_seconds: float | None = None,
) -> str:
    """Call one Gemini model and require a non-empty text response."""
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=DECISION_OUTPUT_TOKEN_LIMIT,
        safety_settings=SAFETY_SETTINGS,
        response_mime_type=response_mime_type,
        response_json_schema=response_json_schema,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.MEDIUM,
        ),
    )
    response = call_gemini_with_retry(
        client=client,
        model=model_name,
        contents=user_message,
        config=config,
        max_retries=max_retries if max_retries is not None else _retry_count_for_model(model_name),
        retry_budget_seconds=retry_budget_seconds,
    )
    response_model_version = getattr(response, "model_version", None)
    if isinstance(response_model_version, str) and response_model_version.strip():
        _MODEL_VERSION_BY_ID[model_name] = response_model_version.strip()
    text = response.text
    if not text or not text.strip():
        raise RuntimeError("응답이 완전히 비어 있음")
    return text


def _call_investment_decision(
    client: genai.Client,
    user_message: str,
    validator: Callable[[str], object],
    *,
    provider: str = "gemini",
    model_name: str | None = None,
    request_budget=None,
    request_metrics: list[dict] | None = None,
) -> object:
    """Run a fail-closed investment decision with one explicit model."""
    stage_name = "1차 포트폴리오 판단"
    model_name = model_name or _decision_model()
    provider_label = "Gemini" if provider == "gemini" else "ChatGPT"
    started_at = time.monotonic()

    def remaining_budget() -> float:
        return max(0.0, RETRY_BUDGET_SECONDS - (time.monotonic() - started_at))

    def generate_text(message: str) -> str:
        if provider == "gemini":
            return _call_model_text(
                client, model_name, message, DECISION_SYSTEM_PROMPT,
                response_mime_type="application/json",
                max_retries=DECISION_MAX_ATTEMPTS,
                response_json_schema=DECISION_RESPONSE_SCHEMA,
                retry_budget_seconds=remaining_budget(),
            )
        attempted = False
        call_started = time.monotonic()

        def before_request():
            nonlocal attempted
            consume_request(request_budget, provider, model_name)
            attempted = True

        try:
            response = client.generate_json(
                model=model_name, user_message=message, instructions=DECISION_SYSTEM_PROMPT,
                json_schema=DECISION_RESPONSE_SCHEMA,
                retry_budget_seconds=remaining_budget(), before_request=before_request,
            )
        except Exception as exc:
            if attempted and request_metrics is not None:
                record_metric(request_metrics, request_budget, {
                    "provider": provider, "model": model_name, "status": "failed",
                    "error_type": type(exc).__name__,
                    "latency_seconds": round(time.monotonic() - call_started, 3),
                })
            raise
        _MODEL_VERSION_BY_ID[model_name] = response.model_version or model_name
        if request_metrics is not None:
            usage = response.usage
            details = usage.get("output_tokens_details") or {}
            record_metric(request_metrics, request_budget, {
                "provider": provider, "model": model_name, "status": "completed",
                "latency_seconds": response.latency_seconds,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "thinking_tokens": details.get("reasoning_tokens") if isinstance(details, dict) else None,
                "total_tokens": usage.get("total_tokens"),
            })
        return response.text

    try:
        print(f"  {stage_name} 모델 시도: {model_name}")
        text = generate_text(user_message)
        try:
            return validator(text)
        except Exception as validation_error:
            print(
                f"    {stage_name} 형식 교정 재시도 1/1: {model_name} - "
                f"{str(validation_error)[:180]}"
            )
            repaired_text = generate_text(
                user_message
                + "\n\n직전 응답은 다음 검증 오류가 있었습니다:\n"
                + str(validation_error)
                + "\n누락된 근거와 필수 필드를 보완하여 요구 형식의 전체 응답을 다시 출력하십시오.",
            )
            return validator(repaired_text)
    except Exception as exc:
        message = str(exc)
        print(f"    {stage_name} 보류: {model_name} - {message[:180]}")
        raise RuntimeError(
            f"{provider_label} 투자 판단 보류. model={model_name}. {message}"
        ) from exc


# ─── 입력 구성 헬퍼 ──────────────────────────────────────────────────────────

def _analysis_text_for_post(post: Dict) -> tuple[str, str]:
    """Use only the per-post summary as the final decision-model input."""
    summary = post.get("summary", "").strip()
    if summary:
        return "1차 요약", summary

    title = post.get("title", "제목 없음")
    raise ValueError(f"요약 없는 글은 투자 판단 입력으로 사용할 수 없습니다: {title}")


def _format_report_date(today_str: str) -> str:
    """LLM이 날짜를 잘못 쓰지 않도록 보고서 날짜를 코드에서 고정한다."""
    match = re.match(r"^\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*$", today_str)
    if not match:
        return today_str

    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _normalize_report_metadata(
    report: str,
    today_str: str,
    post_count: int,
    start_date: str,
    end_date: str,
) -> str:
    """보고서 상단의 결정적 메타데이터는 입력값으로 보정한다."""
    replacements = {
        r"(?m)^\*\*분석 기간:\*\*.*$": f"**분석 기간:** {start_date} ~ {end_date}",
        r"(?m)^\*\*리포트 생성:\*\*.*$": f"**리포트 생성:** {_format_report_date(today_str)}",
        r"(?m)^\*\*학습 글 수:\*\*.*$": f"**학습 글 수:** {post_count}편",
    }

    normalized = report
    for pattern, replacement in replacements.items():
        if re.search(pattern, normalized):
            normalized = re.sub(pattern, replacement, normalized, count=1)

    return normalized


def _structured_context(posts: List[Dict]) -> str:
    """Build the shared blog context for both structured-analysis calls."""
    blocks = []
    for index, post in enumerate(posts, 1):
        label, text = _analysis_text_for_post(post)
        signal_candidates = [
            candidate
            for candidate in post.get("signal_candidates", [])
            if isinstance(candidate, dict)
            and candidate.get("signal_id")
            and candidate.get("evidence_sha256")
        ]
        signal_payload = json.dumps(
            signal_candidates,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        blocks.append(
            f"[{index}/{len(posts)}] 제목: {post['title']}\n"
            f"날짜: {post['date']}\n"
            f"URL: {post.get('url', '')}\n"
            f"{label}:\n{text}\n"
            f"호스트 검증 원문 신호 후보(JSON):\n{signal_payload}\n"
            f"{'─' * 50}"
        )
    return "\n\n".join(blocks)


def _count_tokens(client: genai.Client, model: str, contents: str) -> int | None:
    """Return request tokens when the live client supports token counting."""
    models = getattr(client, "models", None)
    if models is None or not hasattr(models, "count_tokens"):
        return None
    response = models.count_tokens(model=model, contents=contents)
    return int(response.total_tokens)


def _fit_context_to_budget(
    client: genai.Client,
    context: str,
    message_builder: Callable[[str], str],
    *,
    model_name: str | None = None,
) -> str:
    """Keep stored inputs intact and trim only an abnormal transmitted context tail."""
    model = model_name or DECISION_MODEL
    safe_limit = int(MODEL_INPUT_TOKEN_LIMIT * MODEL_INPUT_SAFE_RATIO)
    message = message_builder(context)
    tokens = _count_tokens(client, model, message)
    if tokens is None or tokens <= safe_limit:
        return context

    low, high = 0, len(context)
    suffix = "\n...(전송용 분석 문맥 끝부분 생략)"
    while low < high:
        middle = (low + high + 1) // 2
        candidate = context[:middle] + suffix
        candidate_tokens = _count_tokens(client, model, message_builder(candidate))
        if candidate_tokens is not None and candidate_tokens <= safe_limit:
            low = middle
        else:
            high = middle - 1
    fitted = context[:low] + suffix
    fitted_tokens = _count_tokens(client, model, message_builder(fitted))
    if fitted_tokens is not None and fitted_tokens > safe_limit:
        raise ValueError(
            f"압축된 포트폴리오 상태만으로 모델 입력 예산을 초과합니다: {fitted_tokens} tokens"
        )
    return fitted


def _compact_state_for_inference(current_state: dict | None) -> dict:
    """Keep active decisions and referenced signals; exclude unbounded archives."""
    if not current_state:
        return {}
    state = dict(current_state)
    portfolio = list(state.get("portfolio", []) or [])
    watchlist = list(state.get("watchlist", []) or [])
    referenced_ids = {
        str(signal_id)
        for item in portfolio + watchlist
        for key in ("origin_signal_ids", "linked_signal_ids")
        for signal_id in (item.get(key, []) or [])
        if str(signal_id)
    }
    signals = [
        item
        for item in state.get("signal_events", []) or []
        if item.get("signal_id") in referenced_ids
    ]
    return {
        "schema_version": state.get("schema_version"),
        "portfolio": portfolio,
        "watchlist": watchlist,
        "closed_positions": list(state.get("closed_positions", []) or [])[-10:],
        "decision_history": list(state.get("decision_history", []) or [])[-20:],
        "signal_events": signals,
        "insights": list(state.get("insights", []) or []),
        "last_watchlist_changes": state.get("last_watchlist_changes", {}),
        "last_rebalanced_date": state.get("last_rebalanced_date"),
    }


def _decision_for_pre_provenance_projection(
    decision: AnalysisDecisionV2,
) -> AnalysisDecisionV2:
    """Make a state-valid preview before main.py attaches source events.

    The decision model is shown host-created signal IDs, but the corresponding
    events are deliberately appended only later in ``main.py`` after their
    URL, entity, and direction have been validated.  Applying the raw model
    decision here used to reject a perfectly valid candidate because those
    new IDs were not in the persisted ledger *yet*.  This preview is used
    solely for first-call consistency checks and the deterministic draft
    report; it never changes the decision returned to main.py.
    """
    payload = deepcopy(decision.to_dict())
    for item in payload["portfolio_decisions"]:
        item["linked_signal_ids"] = []
        item["origin_signal_ids"] = []
        item["provenance_status"] = "legacy_unvalidated"
        item["origin_signal_type"] = "LEGACY_UNVALIDATED"
        item["rejected_linked_signal_ids"] = []
    for item in payload["watchlist"]:
        item["linked_signal_ids"] = []
        item["origin_signal_ids"] = []
        item["provenance_status"] = "legacy_unvalidated"
        item["origin_signal_type"] = "LEGACY_UNVALIDATED"
    return parse_analysis_decision(payload)


def analyze_posts_structured(
    posts: List[Dict],
    analysis_date: str,
    current_state: dict | None,
    *,
    is_rebalance: bool = False,
    decision_validator: Callable[[AnalysisDecisionV2], object] | None = None,
    decision_provider: str | None = None,
    decision_model: str | None = None,
    context_mode: str | None = None,
    request_budget=None,
) -> StructuredAnalysisResult:
    """Generate validated decision JSON first, then a Markdown report."""
    if not posts:
        raise ValueError("분석할 포스트가 없습니다.")

    provider = resolve_provider(decision_provider)
    model_name = resolve_model(provider, decision_model, _decision_model())
    selected_context = resolve_context_mode(context_mode)
    request_metrics: list[dict] = []
    context, inference_state = _decision_context_parts(posts, current_state, selected_context)
    run_type = "rebalance" if is_rebalance else "regular"
    if provider == "gemini":
        client = _get_client()
        if getattr(client, "models", None) is not None:
            client = TrackedGeminiClient(client, request_budget, request_metrics)
    else:
        from chatgpt_client import ChatGPTClient
        client = ChatGPTClient.from_env()

    decision_builder = lambda request_context: build_decision_user_message(
        context=request_context,
        analysis_date=analysis_date,
        run_type=run_type,
        current_state=inference_state,
    )
    if provider == "gemini":
        context = _fit_context_to_budget(client, context, decision_builder, model_name=model_name)
    decision_message = decision_builder(context)
    selected_validator = decision_validator
    if selected_context == "focused":
        selected_validator = _focused_source_validator(
            posts, current_state, analysis_date, model_name, decision_validator,
        )
    decision = _call_investment_decision(
        client,
        decision_message,
        lambda text: _parse_and_validate_model_decision_json(
            text,
            current_state,
            selected_validator,
            expected_analysis_date=analysis_date,
            expected_run_type=run_type,
        ),
        provider=provider,
        model_name=model_name,
        request_budget=request_budget,
        request_metrics=request_metrics,
    )
    assert isinstance(decision, AnalysisDecisionV2)
    decision_model_version = _MODEL_VERSION_BY_ID.get(
        model_name,
        model_name,
    )
    decision_payload = decision.to_dict()
    for item in decision_payload["portfolio_decisions"]:
        item["decision_model_id"] = decision_model_version
    decision = parse_analysis_decision(decision_payload)

    projected_state = current_state or {}
    if current_state and current_state.get("schema_version") in {"2.0", "2.1"}:
        projected_state = apply_analysis_decision(
            parse_portfolio_state(current_state),
            _decision_for_pre_provenance_projection(decision),
        ).to_dict()

    # main.py는 구조화 결과로 최종 사용자 보고서를 다시 생성한다. 여기서는
    # 검증된 판단으로 결정론적 보고서만 만들어 불필요한 두 번째 LLM 호출을 막는다.
    report = _build_deterministic_report(
        decision,
        projected_state,
        analysis_date,
    )
    return StructuredAnalysisResult(
        decision=decision,
        report=report,
        decision_model_version=decision_model_version,
        decision_provider=provider,
        request_metrics=request_metrics,
    )


def _decision_context_parts(posts, current_state, context_mode):
    context = _structured_context(posts)
    if context_mode == "baseline":
        return context, _compact_state_for_inference(current_state)
    from decision_context import build_evidence_packet, compact_state_for_decision
    packet = build_evidence_packet(posts, current_state)
    guide = packet if isinstance(packet, str) else json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    return context + "\n\n[원문 신호와 행동 방향 확인]\n" + guide, compact_state_for_decision(current_state)


def preview_decision_input(
    posts, analysis_date, current_state, *, is_rebalance=False, context_mode=None,
) -> str:
    """Build exact pre-transport model input without any network request."""
    mode = resolve_context_mode(context_mode)
    context, inference_state = _decision_context_parts(posts, current_state, mode)
    return build_decision_user_message(
        context=context, analysis_date=analysis_date,
        run_type="rebalance" if is_rebalance else "regular", current_state=inference_state,
    )


def _focused_source_validator(posts, current_state, analysis_date, model_name, custom_validator):
    """Return source errors to the model's existing one-shot correction path."""
    from portfolio_provenance import enrich_decision_provenance, prepare_post_signal_events
    from portfolio_runtime import security_key, validate_rebalance_coverage
    from decision_context import ambiguous_source_signal_ids
    _, source_events = prepare_post_signal_events(posts, created_at=analysis_date, model_id=model_name)
    ambiguous_ids = ambiguous_source_signal_ids(posts)

    def validate(decision):
        if custom_validator is not None:
            try:
                candidate = custom_validator(decision)
            except ValueError as exc:
                raise ValueError(str(exc) + _source_repair_feedback(decision, current_state, source_events)) from exc
            if isinstance(candidate, AnalysisDecisionV2):
                decision = candidate
        validation_state = current_state if current_state and current_state.get("schema_version") in {"2.0", "2.1"} else {
            "schema_version": "2.0", "portfolio": [], "watchlist": [],
            "closed_positions": [], "decision_history": [], "insights": [], "signal_events": [],
        }
        state = parse_portfolio_state(validation_state)
        validate_rebalance_coverage(state, decision)
        enriched, events = enrich_decision_provenance(
            decision, source_events, created_at=analysis_date, model_id=model_name,
        )
        try:
            projected = apply_analysis_decision(state, enriched, new_signal_events=events)
        except ValueError as exc:
            raise ValueError(str(exc) + _source_repair_feedback(decision, current_state, source_events)) from exc
        _validate_direction_review_exposure(enriched, validation_state, events, ambiguous_ids)
        existing_keys = {security_key(item) for item in state.portfolio}
        for item in projected.portfolio:
            if security_key(item) not in existing_keys and item.get("provenance_status") != "verified":
                raise ValueError(f"원문 신호로 검증되지 않은 신규 편입: {item.get('code')}")
        return decision

    return validate


def _source_repair_feedback(decision, current_state, events):
    """Explain rejected links without changing evidence or approving a trade."""
    from portfolio_provenance import _direction_matches_action, _identity_matches
    from portfolio_runtime import security_key
    by_id = {event['signal_id']: event for event in events}
    current = {security_key(item): item for item in (current_state or {}).get('portfolio', [])}
    enriched_rows = None
    if isinstance(decision, AnalysisDecisionV2):
        from portfolio_provenance import enrich_decision_provenance
        enriched, _ = enrich_decision_provenance(
            decision, events, created_at=decision.analysis_date, model_id='repair-diagnostic-only',
        )
        enriched_rows = enriched.portfolio_decisions
    rows = []
    for index, item in enumerate(decision.portfolio_decisions):
        old = current.get(security_key(item), {})
        if not old and enriched_rows is not None and enriched_rows[index].get('provenance_status') != 'verified':
            rows.append({'decision_index': index, 'code': item.get('code'), 'name': item.get('name'),
                         'action': item.get('action'), 'reasons': [
                             '신규 편입인데 현재 원문 신호에서 대상·방향·근거 연결이 검증되지 않음. 다른 보유 종목의 연결 오류와 함께 이번 교정에서 검토하십시오.',
                             '원문에 없는 직접 추천이나 코드를 만들지 마십시오. 적합한 근거를 검증할 수 없다면 편입하지 말고 관심종목과 필요한 추가 검토에 남기십시오.',
                         ]})
        old_ids = set((old.get('linked_signal_ids') or []) + (old.get('origin_signal_ids') or []))
        for signal_id in item.get('linked_signal_ids') or []:
            if signal_id in old_ids:
                continue
            event = by_id.get(signal_id)
            reasons = []
            if event is None:
                reasons.append('새 입력에 없고 이 보유 종목의 기존 연결도 아닌 ID')
            else:
                if not _direction_matches_action(item, event):
                    reasons.append('근거의 방향이 제안 행동과 불일치; 반대 근거를 보유·매수의 지지 근거로 연결하지 마십시오')
                if not _identity_matches(item, event) and not (
                    item.get('decision_actor') == 'AI' and item.get('asset_type') == 'etf'
                    and item.get('source_scope') == 'sector_only' and str(item.get('investment_rationale') or '').strip()
                ):
                    reasons.append('원문 대상이 이 종목과 다름; ETF 추론은 AI/sector_only와 명시적인 연결 논리가 필요하며 개별주로 임의 전용할 수 없음')
                if event.get('signal_type') not in {'MER_DIRECT', 'MER_THESIS'}:
                    reasons.append('직접 판단·방향성 논지가 아닌 신호')
                urls = {post.get('url') for post in item.get('evidence_posts') or []}
                if event.get('post_url') not in urls:
                    reasons.append('근거 글 목록에 이 신호의 원문 URL이 없음')
            if reasons:
                rows.append({'decision_index': index, 'code': item.get('code'), 'name': item.get('name'),
                             'action': item.get('action'), 'signal_id': signal_id,
                             'source_entity': event.get('entity') if event else None,
                             'source_direction': event.get('direction') if event else None,
                             'reasons': reasons})
    if not rows:
        return ''
    return '\n근거 연결 교정 상세: ' + json.dumps(rows, ensure_ascii=False, separators=(',', ':')) + (
        '\n근거의 원문·대상·방향을 바꾸거나 ID를 만들어 통과시키지 마십시오. 반대 근거는 key_risks·무효화 검토에 설명하십시오. '
        '기존 보유를 유지할 근거가 실제로 유효하면 그 종목의 기존 연결을 보존하고, 무효화됐으면 원문에 맞는 축소·매도를 판단하십시오.'
    )


def _validate_direction_review_exposure(decision, current_state, events, ambiguous_ids):
    """Mixed direction labels alone cannot justify increasing exposure.

    Canonical origins from enrich_decision_provenance, rather than raw model
    links, determine the basis. Existing exposure and protective reductions are
    left to the existing portfolio policy; this does not infer a bearish view.
    """
    from portfolio_runtime import security_key
    if not ambiguous_ids:
        return
    current = {security_key(item): float(item.get("proposed_weight") or 0) for item in current_state.get("portfolio", [])}
    event_by_id = {item["signal_id"]: item for item in events}
    for item in decision.portfolio_decisions:
        if float(item.get("proposed_weight") or 0) <= current.get(security_key(item), 0) + 1e-9:
            continue
        roots = list(item.get("origin_signal_ids") or item.get("linked_signal_ids") or [])
        parents = set()
        visited = set()
        while roots:
            signal_id = roots.pop()
            if signal_id in visited:
                continue
            visited.add(signal_id)
            event = event_by_id.get(signal_id)
            if event is None:
                continue
            if event.get("signal_type") == "AI_INFERRED":
                roots.extend(event.get("parent_signal_ids") or [])
            elif event.get("signal_type") in {"MER_DIRECT", "MER_THESIS"} and event.get("direction") == "bullish":
                parents.add(signal_id)
        if parents & ambiguous_ids and not parents - ambiguous_ids:
            raise ValueError(f"방향 검토가 필요한 근거만으로 신규·추가 투자할 수 없음: {item.get('code')}. 원문 대상·가격 방향을 구분하거나 독립된 적합 근거를 사용하십시오.")


def _build_deterministic_report(
    decision: AnalysisDecisionV2,
    projected_state: dict,
    analysis_date: str,
) -> str:
    """Build a Markdown report from validated state when the second LLM call fails."""
    insights = projected_state.get("insights", decision.insights)
    portfolio = projected_state.get("portfolio", [])
    watchlist = projected_state.get("watchlist", [])
    closed = projected_state.get("closed_positions", [])
    changes = decision.portfolio_decisions

    lines = [
        f"# 메르AI 포트폴리오 보고서 ({analysis_date})",
        "",
        "## 핵심 인사이트",
        "",
    ]
    if insights:
        for item in insights:
            lines.append(f"### {item.get('title', '제목 없음')}")
            lines.append("")
            lines.append(str(item.get("summary", "")).strip() or "요약 없음")
            implication = str(item.get("investment_implication", "")).strip()
            if implication:
                lines.append("")
                lines.append(f"**투자 시사점:** {implication}")
            evidence = item.get("evidence_posts", [])
            if evidence:
                lines.append("")
                lines.append("**근거 글:**")
                for post in evidence:
                    lines.append(
                        f"- [{post.get('title', '제목 없음')}]({post.get('url', '')})"
                        f" · {post.get('published_date', '')}"
                    )
            lines.append("")
    else:
        lines.extend(["표시할 핵심 인사이트가 없습니다.", ""])

    lines.extend([
        "## 현재 모델 포트폴리오",
        "",
        "메르 블로거의 실제 보유 내역이 아니라, 블로그 근거와 AI 해석을 구분해 만든 모델 포트폴리오입니다.",
        "",
        "| 종목 | 코드 | 판단 주체 | 행동 | 비중 | 근거 |",
        "|---|---:|---|---|---:|---|",
    ])
    if portfolio:
        for item in portfolio:
            lines.append(
                f"| {item.get('name', '')} | {item.get('code', '')} | "
                f"{item.get('decision_actor', '')} | {item.get('action', '')} | "
                f"{item.get('proposed_weight', 0):g}% | {item.get('change_reason', '')} |"
            )
    else:
        lines.append("| 편입 종목 없음 |  |  |  |  |  |")

    lines.extend([
        "",
        "## Watchlist",
        "",
        "| 항목 | 코드 | 상태 | 관찰 이유 |",
        "|---|---:|---|---|",
    ])
    if watchlist:
        for item in watchlist:
            lines.append(
                f"| {item.get('name', '')} | {item.get('code', '')} | "
                f"{item.get('status', '')} | {item.get('observation_reason', '')} |"
            )
    else:
        lines.append("| 표시할 항목 없음 |  |  |  |")

    lines.extend([
        "",
        "## 변경 및 종료 포지션",
        "",
        "### 이번 분석 변경",
        "",
        "| 종목 | 코드 | 행동 | 이전 비중 | 제안 비중 | 변경 이유 |",
        "|---|---:|---|---:|---:|---|",
    ])
    if changes:
        for item in changes:
            previous = item.get("previous_weight")
            previous_text = "-" if previous is None else f"{previous:g}%"
            lines.append(
                f"| {item.get('name', '')} | {item.get('code', '')} | "
                f"{item.get('action', '')} | {previous_text} | "
                f"{item.get('proposed_weight', 0):g}% | {item.get('change_reason', '')} |"
            )
    else:
        lines.append("| 변경 없음 |  |  |  |  |  |")

    lines.extend([
        "",
        "### 종료 포지션",
        "",
        "| 종목 | 코드 | 종료일 | 종료 이유 |",
        "|---|---:|---|---|",
    ])
    if closed:
        for item in closed:
            lines.append(
                f"| {item.get('name', '')} | {item.get('code', '')} | "
                f"{item.get('closed_date', '')} | {item.get('close_reason', '')} |"
            )
    else:
        lines.append("| 종료 포지션 없음 |  |  |  |")

    report = "\n".join(lines)
    return _validate_markdown_report(report)


def _validate_markdown_report(report: str) -> str:
    required_headers = [
        "핵심 인사이트",
        "현재 모델 포트폴리오",
        "Watchlist",
        "변경 및 종료 포지션",
    ]
    missing_headers = [header for header in required_headers if header not in report]
    if missing_headers:
        raise ValueError("필수 보고서 섹션 누락: " + ", ".join(missing_headers))
    if len(report) > 20_000:
        raise ValueError(f"보고서가 비정상적으로 깁니다: {len(report)}자")
    longest_line = max((len(line) for line in report.splitlines()), default=0)
    if longest_line > 3_000:
        raise ValueError(f"보고서 한 줄이 비정상적으로 깁니다: {longest_line}자")
    return report


def _parse_model_decision_json(text: str) -> AnalysisDecisionV2:
    """Exclude untradeable portfolio suggestions before strict schema validation."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    payload = json.loads(stripped)
    decisions = payload.get("portfolio_decisions", [])
    if not isinstance(decisions, list):
        return parse_analysis_decision(payload)
    tradeable = []
    for item in decisions:
        if (
            isinstance(item, dict)
            and item.get("asset_type") in {"stock", "etf"}
            and (not isinstance(item.get("code"), str) or not item["code"].strip())
        ):
            print(
                "    거래 불가능 포트폴리오 제안 제외: "
                + str(item.get("name") or "이름 없음")
            )
            continue
        tradeable.append(item)
    payload["portfolio_decisions"] = tradeable
    return parse_analysis_decision(payload)


def _parse_and_validate_model_decision_json(
    text: str,
    current_state: dict | None,
    decision_validator: Callable[[AnalysisDecisionV2], object] | None = None,
    *,
    expected_analysis_date: str | None = None,
    expected_run_type: str | None = None,
) -> AnalysisDecisionV2:
    """Require the model decision to produce an applicable target portfolio."""
    decision = _parse_model_decision_json(text)
    if expected_analysis_date is not None and decision.analysis_date != expected_analysis_date:
        raise ValueError(
            f"analysis_date must be {expected_analysis_date}, got {decision.analysis_date}"
        )
    if expected_run_type is not None and decision.run_type != expected_run_type:
        raise ValueError(f"run_type must be {expected_run_type}, got {decision.run_type}")
    if decision_validator is not None:
        validated = decision_validator(decision)
        if isinstance(validated, AnalysisDecisionV2):
            decision = validated
    if current_state and "schema_version" in current_state:
        state = parse_portfolio_state(current_state)
        current_by_key = {
            (
                str(item.get("asset_type") or "").lower(),
                str(item.get("market") or "").upper(),
                str(item.get("code") or "").upper(),
            ): item
            for item in state.portfolio
        }
        for item in decision.portfolio_decisions:
            key = (
                str(item.get("asset_type") or "").lower(),
                str(item.get("market") or "").upper(),
                str(item.get("code") or "").upper(),
            )
            current = current_by_key.get(key)
            previous = item.get("previous_weight")
            if current is None:
                if previous not in (None, 0, 0.0):
                    raise ValueError(f"new position {key} previous_weight must be null or 0")
            elif previous is None or abs(float(previous) - float(current["proposed_weight"])) > 1e-9:
                raise ValueError(
                    f"existing position {key} previous_weight must equal current target "
                    f"{current['proposed_weight']}"
                )
            proposed = float(item.get("proposed_weight") or 0.0)
            previous_value = float(previous or 0.0)
            action = item.get("action")
            if action == "비중확대" and proposed <= previous_value:
                raise ValueError(f"{key} 비중확대 must increase proposed_weight")
            if action == "비중축소" and proposed >= previous_value:
                raise ValueError(f"{key} 비중축소 must decrease proposed_weight")
            if action == "보유" and abs(proposed - previous_value) > 1e-9:
                raise ValueError(f"{key} 보유 must keep proposed_weight unchanged")
            if action == "매도" and proposed != 0.0:
                raise ValueError(f"{key} 매도 must set proposed_weight to 0")
        apply_analysis_decision(
            state,
            _decision_for_pre_provenance_projection(decision),
        )
    return decision


# ─── 메인 분석 함수 ───────────────────────────────────────────────────────────

def analyze_posts(
    posts: List[Dict],
    today_str: str,
    run_mode: str = "scheduled",
    current_holdings_text: str = "",
    is_rebalance: bool = False,
) -> str:
    """
    [2단계 분석]
    수집된 글의 1차 요약을 병합한 뒤 최종 포트폴리오 리포트를 생성한다.
    """
    if not posts:
        raise ValueError("분석할 포스트가 없습니다.")

    # 1. 요약 캐시가 있으면 사용하고, 없으면 API 호출 없이 원문을 사용한다.
    print("\n[3/7-1] 1단계: 블로그 글 요약 캐시 로드 중...")
    summarized_blocks = []
    for i, post in enumerate(posts, 1):
        label, text = _analysis_text_for_post(post)
        if label == "원문":
            print(f"      요약 캐시 없음, 원문 사용: {post.get('title', '제목 없음')[:30]}...")
            
        summarized_blocks.append(
            f"[{i}/{len(posts)}] 제목: {post['title']}\n"
            f"날짜: {post['date']}\n"
            f"{label}:\n{text}\n"
            f"{'─' * 50}"
        )

    # 요약된 블록만 병합해 최종 분석 입력을 줄인다.
    reduced_context = "\n\n".join(summarized_blocks)
    
    start_date = posts[-1]["date"]
    end_date = posts[0]["date"]

    # 2단계: 요약본을 전달하여 최종 포트폴리오 리포트 생성
    user_message = build_user_message(
        context=reduced_context,
        today_str=today_str,
        post_count=len(posts),
        start_date=start_date,
        end_date=end_date,
        run_mode=run_mode,
        current_holdings_text=current_holdings_text,
        is_rebalance=is_rebalance,
    )

    total_chars = len(user_message)
    print(f"\n[3/7-2] 2단계: 최종 분석 입력 크기: {total_chars:,}자")
    models = _model_sequence()
    print(f"  최종 분석 모델 순서: {' -> '.join(models)}")

    client = _get_client()
    last_model = models[-1]
    last_error = ""

    for model_name in models:
        success, result = _try_model(client, model_name, user_message)
        if success:
            result = _normalize_report_metadata(result, today_str, len(posts), start_date, end_date)
            print(f"  최종 분석 완료: {model_name} (출력: {len(result):,}자)")
            return result

        last_model = model_name
        last_error = result

        if "필수 세션 누락" in result or "응답 길이가 너무 짧" in result:
            print("  필수 섹션이 누락되어 형식 지시를 강화해 같은 모델로 한 번 재시도합니다.")
            retry_message = (
                user_message
                + "\n\n중요: 출력은 반드시 '# 메르AI 포트폴리오 리포트'로 시작하고, "
                + "'## 📌 시장 분석 핵심 인사이트', '## 📊 포트폴리오 추천', "
                + "'## 💬 한 줄 코멘트' 섹션을 모두 포함해야 합니다. "
                + "토큰이 부족하면 각 항목을 짧게 줄이더라도 섹션을 생략하지 마세요."
            )
            success, result = _try_model(client, model_name, retry_message, max_retries=1)
            if success:
                result = _normalize_report_metadata(result, today_str, len(posts), start_date, end_date)
                print(f"  최종 분석 완료: {model_name} (형식 재시도, 출력: {len(result):,}자)")
                return result
            last_error = result

        if model_name != models[-1]:
            print(f"  {model_name} 실패 -> 다음 모델로 fallback합니다.")

    if is_daily_quota_error(last_error):
        print(f"  {last_model} daily quota exceeded.")

    raise RuntimeError(
        f"{' -> '.join(models)} 호출 실패. 기존 리포트를 유지합니다.\n"
        f"Google API 오류: {last_error}\n\n"
        "해결 방법:\n"
        "1. GitHub Actions 로그에서 quota 또는 rate limit 원인을 확인하세요.\n"
        "2. 필요하면 Google AI Studio 결제 연동, 실행 빈도 조정, 또는 호출 간격 조정을 검토하세요."
    )


# ─── 직접 실행 테스트 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    from fetch_mer import fetch_recent_posts

    print("=== 분석 모듈 테스트 ===")
    posts = fetch_recent_posts(days=7)
    if not posts:
        print("수집된 글 없음")
    else:
        today_str = datetime.now().strftime("%Y년 %m월 %d일")
        report = analyze_posts(posts, today_str)
        print("\n=== 리포트 미리보기 (앞 1000자) ===")
        print(report[:1000])
