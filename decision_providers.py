"""Explicit decision-provider selection and per-generation request accounting."""

from __future__ import annotations

import os
import time


class GenerationRequestLimit(RuntimeError):
    """A local experiment allowance, distinct from a provider rate limit."""


def resolve_provider(value: str | None = None) -> str:
    provider = (value if value is not None else os.environ.get("MER_DECISION_PROVIDER", "gemini")).strip().lower()
    if provider not in {"gemini", "chatgpt"}:
        raise ValueError("MER_DECISION_PROVIDER must be gemini or chatgpt")
    return provider


def resolve_context_mode(value: str | None = None) -> str:
    mode = (value if value is not None else os.environ.get("MER_DECISION_CONTEXT", "baseline")).strip().lower()
    if mode not in {"baseline", "focused"}:
        raise ValueError("MER_DECISION_CONTEXT must be baseline or focused")
    return mode


def resolve_model(provider: str, value: str | None, gemini_default: str) -> str:
    model = value if value is not None else (
        gemini_default if provider == "gemini" else os.environ.get("CHATGPT_DECISION_MODEL", "gpt-6.1-sol")
    )
    model = model.strip()
    if not model:
        raise ValueError("A decision model must be selected explicitly")
    return model


def consume_request(budget, provider: str, model: str) -> None:
    if budget is not None:
        budget.consume(provider, model)


def record_metric(observations, budget, metric) -> None:
    observations.append(metric)
    recorder = getattr(budget, "record_response", None)
    if callable(recorder):
        recorder(metric)


class _TrackedGeminiModels:
    def __init__(self, models, budget, observations):
        self._models = models
        self._budget = budget
        self._observations = observations

    def __getattr__(self, name):
        return getattr(self._models, name)

    def generate_content(self, **kwargs):
        model = kwargs["model"]
        consume_request(self._budget, "gemini", model)
        started = time.monotonic()
        observation = {"provider": "gemini", "model": model}
        try:
            response = self._models.generate_content(**kwargs)
            usage = getattr(response, "usage_metadata", None)
            for key, attribute in (
                ("input_tokens", "prompt_token_count"),
                ("output_tokens", "candidates_token_count"),
                ("thinking_tokens", "thoughts_token_count"),
                ("total_tokens", "total_token_count"),
            ):
                value = getattr(usage, attribute, None)
                if isinstance(value, int):
                    observation[key] = value
            observation["status"] = "completed"
            return response
        except Exception as exc:
            observation.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            observation["latency_seconds"] = round(time.monotonic() - started, 3)
            record_metric(self._observations, self._budget, observation)


class TrackedGeminiClient:
    """Count actual generate calls, including retries inside gemini_utils."""

    def __init__(self, client, budget, observations):
        self.models = _TrackedGeminiModels(client.models, budget, observations)
