"""
Gemini API 호출 보조 유틸리티.

무료 티어에서는 rate limit이 자주 병목이 되므로 모델별 최소 호출 간격을
보수적으로 지킨다. 품질 정책은 호출부에서 결정하고, 이 모듈은 재시도와
대기만 담당한다.
"""

import os
import random
import re
import time
from collections import defaultdict
from decision_providers import GenerationRequestLimit


DEFAULT_HTTP_TIMEOUT_MS = int(
    os.environ.get("GEMINI_DECISION_HTTP_TIMEOUT_MS", "120000")
)
SUMMARY_HTTP_TIMEOUT_MS = int(
    os.environ.get("GEMINI_SUMMARY_HTTP_TIMEOUT_MS", "60000")
)
RETRY_BUDGET_SECONDS = float(
    os.environ.get("GEMINI_RETRY_BUDGET_SECONDS", "180")
)

PERMANENT_ERROR = "GEMINI_PERMANENT"
RATE_LIMIT_ERROR = "GEMINI_RATE_LIMIT"
TRANSIENT_ERROR = "GEMINI_TRANSIENT"

MODEL_MIN_INTERVALS = {
    "gemini-3.1-flash-lite": 8.0,
    "gemini-3.5-flash": 8.0,
}

DEFAULT_MIN_INTERVAL = 8.0

_last_call_at = defaultdict(float)


def get_min_interval(model: str) -> float:
    return MODEL_MIN_INTERVALS.get(model, DEFAULT_MIN_INTERVAL)


def is_rate_limit_error(message: str) -> bool:
    msg = message.lower()
    return any(token in msg for token in ("429", "resource", "exhausted", "quota", "rate", "limit"))


def is_permanent_error(message: str) -> bool:
    """Return true for configuration/auth/model errors that retries cannot fix."""
    msg = message.lower()
    return any(
        token in msg
        for token in (
            "400",
            "401",
            "403",
            "404",
            "invalid_argument",
            "permission_denied",
            "unauthenticated",
            "not found",
        )
    )


def is_transient_error(message: str) -> bool:
    msg = message.lower()
    return is_rate_limit_error(message) or is_server_busy_error(message) or any(
        token in msg
        for token in (
            "timeout",
            "timed out",
            "read timeout",
            "server disconnected",
            "disconnected without sending a response",
            "connection error",
            "connecterror",
            "connection reset",
            "connection refused",
            "remoteprotocolerror",
            "remote protocol",
            "name resolution",
            "dns",
            "network is unreachable",
            "temporarily unavailable",
        )
    )


def is_server_busy_error(message: str) -> bool:
    msg = message.lower()
    return any(
        token in msg
        for token in (
            "408",
            "500",
            "502",
            "503",
            "504",
            "unavailable",
            "deadline_exceeded",
            "high demand",
            "try again later",
        )
    )


def classify_gemini_error(message: str) -> str:
    if is_permanent_error(message):
        return PERMANENT_ERROR
    if is_rate_limit_error(message):
        return RATE_LIMIT_ERROR
    if is_server_busy_error(message) or is_transient_error(message):
        return TRANSIENT_ERROR
    return "GEMINI_UNKNOWN"


def is_daily_quota_error(message: str) -> bool:
    msg = message.lower()
    return any(
        token in msg
        for token in (
            "perday",
            "requestsperday",
            "tokensperday",
            "daily quota",
            "quota exceeded",
        )
    )


def parse_retry_delay(message: str) -> float | None:
    patterns = (
        r"retry in ([\d.]+)s",
        r"retryDelay': '(\d+)s'",
        r'"retryDelay"\s*:\s*"(\d+)s"',
        r"retry_delay\s*\{\s*seconds:\s*(\d+)",
        r"retryDelay[^\d]+(\d+)\s*s",
    )
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1)) + 1.0
            except ValueError:
                return None
    return None


def wait_for_model_slot(model: str) -> None:
    min_interval = get_min_interval(model)
    elapsed = time.monotonic() - _last_call_at[model]
    if elapsed < min_interval:
        wait_sec = min_interval - elapsed
        print(f"      Waiting {wait_sec:.1f}s before {model} call.")
        time.sleep(wait_sec)


def _model_slot_wait_seconds(model: str) -> float:
    return max(
        0.0,
        get_min_interval(model) - (time.monotonic() - _last_call_at[model]),
    )


def mark_model_called(model: str) -> None:
    _last_call_at[model] = time.monotonic()


def _raise_classified_error(model: str, message: str, error: Exception) -> None:
    category = classify_gemini_error(message)
    raise RuntimeError(f"{category} model={model}: {message}") from error


def _log_response_metadata(model: str, response, elapsed: float) -> None:
    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", None)
    output_tokens = getattr(usage, "candidates_token_count", None)
    thinking_tokens = getattr(usage, "thoughts_token_count", None)
    print(
        "      Gemini response: "
        f"model={getattr(response, 'model_version', None) or model}, "
        f"response_id={getattr(response, 'response_id', None) or '-'}, "
        f"input_tokens={prompt_tokens if prompt_tokens is not None else '-'}, "
        f"output_tokens={output_tokens if output_tokens is not None else '-'}, "
        f"thinking_tokens={thinking_tokens if thinking_tokens is not None else '-'}, "
        f"latency={elapsed:.2f}s"
    )


def _config_with_timeout(config, timeout_ms: int):
    """Clone a google-genai request config with a per-call timeout when supported."""
    model_copy = getattr(type(config), "model_copy", None)
    if not callable(model_copy):
        return config

    from google.genai import types

    existing_http = getattr(config, "http_options", None)
    if existing_http is not None and callable(getattr(type(existing_http), "model_copy", None)):
        existing_timeout = getattr(existing_http, "timeout", None)
        if existing_timeout is not None:
            timeout_ms = min(timeout_ms, int(existing_timeout))
        retry_options = getattr(existing_http, "retry_options", None)
        http_options = existing_http.model_copy(update={
            "timeout": max(1, timeout_ms),
            "retry_options": retry_options or types.HttpRetryOptions(attempts=1),
        })
    else:
        http_options = types.HttpOptions(
            timeout=max(1, timeout_ms),
            retry_options=types.HttpRetryOptions(attempts=1),
        )
    return config.model_copy(update={"http_options": http_options})


def generate_content_with_retry(
    client,
    model: str,
    contents,
    config,
    max_retries: int = 3,
    *,
    http_timeout_ms: int | None = None,
    retry_budget_seconds: float | None = None,
):
    """Call Gemini with one bounded retry layer and stable error markers."""
    attempts = max(1, max_retries)
    backoff = get_min_interval(model)
    budget_seconds = (
        RETRY_BUDGET_SECONDS
        if retry_budget_seconds is None
        else max(0.0, float(retry_budget_seconds))
    )
    started_at = time.monotonic()
    for attempt in range(attempts):
        remaining = budget_seconds - (time.monotonic() - started_at)
        slot_wait = _model_slot_wait_seconds(model)
        if remaining <= 0 or slot_wait >= remaining:
            raise RuntimeError(
                f"{TRANSIENT_ERROR} model={model}: retry wall-clock budget exhausted"
            )
        wait_for_model_slot(model)
        remaining = budget_seconds - (time.monotonic() - started_at)
        if remaining <= 0:
            raise RuntimeError(
                f"{TRANSIENT_ERROR} model={model}: retry wall-clock budget exhausted"
            )
        call_config = config
        if http_timeout_ms is not None:
            call_config = _config_with_timeout(
                config,
                min(int(http_timeout_ms), max(1, int(remaining * 1000))),
            )
        call_started_at = time.monotonic()
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=call_config,
            )
            mark_model_called(model)
            _log_response_metadata(model, response, time.monotonic() - call_started_at)
            return response
        except GenerationRequestLimit:
            raise
        except Exception as e:
            mark_model_called(model)
            message = str(e)
            if is_permanent_error(message):
                _raise_classified_error(model, message, e)
            if not is_transient_error(message):
                raise

            category = classify_gemini_error(message)
            print(
                f"      Retryable Gemini API error: {category} "
                f"model={model} ({attempt + 1}/{attempts})"
            )
            if attempt == attempts - 1:
                _raise_classified_error(model, message, e)

            retry_delay = parse_retry_delay(message)
            if is_daily_quota_error(message):
                _raise_classified_error(model, message, e)

            base_wait = max(retry_delay or backoff, get_min_interval(model))
            wait_sec = base_wait + random.uniform(0.0, min(base_wait * 0.25, 5.0))
            elapsed = time.monotonic() - started_at
            if elapsed + wait_sec >= budget_seconds:
                _raise_classified_error(model, message, e)
            print(f"      Waiting {wait_sec:.1f}s before retry.")
            time.sleep(wait_sec)
            backoff = min(backoff * 1.5, 120.0)

    raise RuntimeError(f"{TRANSIENT_ERROR} model={model}: call failed after {attempts} attempts")
