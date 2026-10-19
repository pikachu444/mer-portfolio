"""Responses streaming client for the official ChatGPT subscription OAuth flow."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Callable

import requests

from chatgpt_auth import ChatGPTAuth, ChatGPTAuthError, CredentialStore, RESOURCE


class ChatGPTClientError(RuntimeError):
    """Safe transport/inference errors; never contains response body or tokens."""


@dataclass(frozen=True)
class ChatGPTResponse:
    text: str
    model_version: str
    usage: dict
    latency_seconds: float


def _events(lines):
    """Parse SSE data frames, including multi-line frames and final EOF data."""
    data = []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if line == "":
            if data:
                yield "\n".join(data)
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield "\n".join(data)


def _safe_code(value) -> str:
    # Structured error codes can inform recovery, raw messages can contain input.
    if isinstance(value, str) and value and all(c.isascii() and (c.isalnum() or c == "_") for c in value):
        return value[:100]
    return "unknown_error"


class ChatGPTClient:
    def __init__(self, auth: ChatGPTAuth | None = None, *, session=None, before_request: Callable | None = None):
        self.auth = auth or ChatGPTAuth()
        self.session = session or requests.Session()
        self.before_request = before_request
        self._models = None

    @classmethod
    def from_env(cls, **kwargs):
        """Use this app's selected OAuth account; never fall back to API keys."""
        return cls(auth=ChatGPTAuth(CredentialStore()), **kwargs)

    def list_models(self, *, refresh: bool = False) -> list[dict]:
        if self._models is None or refresh:
            token = self.auth.access_token()
            try:
                response = self.session.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + token},
                                            timeout=30, allow_redirects=False)
            except requests.RequestException:
                raise ChatGPTClientError("ChatGPT model catalog could not reach OpenAI.") from None
            try:
                if response.status_code != 200:
                    raise ChatGPTClientError(f"ChatGPT model catalog failed (HTTP {response.status_code}).")
                try:
                    payload = response.json()
                except ValueError:
                    raise ChatGPTClientError("ChatGPT model catalog returned invalid JSON.") from None
                models = payload.get("models") if isinstance(payload, dict) else None
                if not isinstance(models, list):
                    raise ChatGPTClientError("ChatGPT model catalog has an invalid format.")
                self._models = [
                    {"slug": item["slug"], "display_name": item.get("display_name", item["slug"])}
                    for item in models if isinstance(item, dict) and item.get("visibility") == "list"
                    and isinstance(item.get("slug"), str) and item["slug"]
                ]
            finally:
                response.close()
        return [dict(item) for item in self._models]

    def generate_json(
        self, model: str, user_message: str, instructions: str, json_schema: dict | None,
        retry_budget_seconds: float = 180, before_request: Callable | None = None,
    ) -> ChatGPTResponse:
        """One generation attempt only. The caller owns retries and validation.

        Existing Gemini schemas contain optional properties. JSON-object mode
        keeps their semantics; the authoritative portfolio validator remains in
        analyze.py, instead of rewriting that schema into a stricter contract.
        """
        started = time.monotonic()
        if retry_budget_seconds <= 0:
            raise ChatGPTClientError("ChatGPT request time budget was exhausted.")
        if model not in {item["slug"] for item in self.list_models()}:
            raise ChatGPTClientError("Requested model is not available to the selected ChatGPT account; run python chatgpt_auth.py models.")
        token = self.auth.access_token()
        schema_instructions = "\n\nReturn one JSON object without Markdown."
        if json_schema is not None:
            schema_instructions += " Follow this exact portfolio JSON schema; optional fields remain optional:\n" + json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
        payload = {
            "model": model,
            "instructions": instructions + schema_instructions,
            "input": [{"role": "user", "content": user_message}],
            "store": False, "stream": True,
            "text": {"format": {"type": "json_object"}},
        }
        remaining = retry_budget_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise ChatGPTClientError("ChatGPT request time budget was exhausted before inference.")
        callback = before_request or self.before_request
        if callback:
            callback()  # Consume request budget immediately before the actual POST.
        try:
            response = self.session.post(
                RESOURCE + "/responses", headers={"Authorization": "Bearer " + token, "Accept": "text/event-stream"},
                json=payload, stream=True, timeout=(min(10, remaining), min(120, remaining)), allow_redirects=False,
            )
        except requests.RequestException:
            raise ChatGPTClientError("ChatGPT inference connection failed; no automatic retry was made.") from None
        chunks = []
        completed = None
        try:
            if response.status_code != 200:
                raise ChatGPTClientError(f"ChatGPT inference failed (HTTP {response.status_code}); no automatic retry was made.")
            for raw in _events(response.iter_lines(decode_unicode=True)):
                if time.monotonic() - started > retry_budget_seconds:
                    raise ChatGPTClientError("ChatGPT inference exceeded its time budget.")
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except ValueError:
                    raise ChatGPTClientError("ChatGPT returned an invalid streaming event.") from None
                if not isinstance(event, dict):
                    raise ChatGPTClientError("ChatGPT returned an invalid streaming object.")
                event_type = event.get("type")
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if not isinstance(delta, str):
                        raise ChatGPTClientError("ChatGPT returned an invalid text delta.")
                    chunks.append(delta)
                elif event_type == "response.completed":
                    completed = event.get("response")
                    if not isinstance(completed, dict) or completed.get("status") != "completed":
                        raise ChatGPTClientError("ChatGPT completion event did not contain a completed response.")
                    break
                elif event_type in {"response.failed", "response.incomplete", "error"}:
                    details = event.get("response") or {}
                    if not isinstance(details, dict):
                        raise ChatGPTClientError("ChatGPT returned an invalid failure event.")
                    error = details.get("error") or event.get("error") or {}
                    code = _safe_code(error.get("code") if isinstance(error, dict) else None)
                    raise ChatGPTClientError(f"ChatGPT {event_type}: {code}.")
            if completed is None:
                raise ChatGPTClientError("ChatGPT stream ended without response.completed; partial output was discarded.")
            # The terminal object is authoritative and may carry output without deltas.
            output = []
            items = completed.get("output", [])
            if not isinstance(items, list):
                raise ChatGPTClientError("ChatGPT completed response output was invalid.")
            for item in items:
                if isinstance(item, dict) and item.get("type") == "message":
                    contents = item.get("content", [])
                    if not isinstance(contents, list):
                        raise ChatGPTClientError("ChatGPT completed message content was invalid.")
                    for content in contents:
                        if isinstance(content, dict) and content.get("type") == "refusal":
                            raise ChatGPTClientError("ChatGPT refused this request.")
                        if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                            output.append(content["text"])
            text = "".join(output) if output else "".join(chunks)
            if not text.strip():
                raise ChatGPTClientError("ChatGPT completed without portfolio output.")
            usage = completed.get("usage")
            version = completed.get("model")
            return ChatGPTResponse(text=text, model_version=version if isinstance(version, str) and version else model,
                                   usage=usage if isinstance(usage, dict) else {}, latency_seconds=time.monotonic() - started)
        except requests.RequestException:
            raise ChatGPTClientError("ChatGPT stream was interrupted; partial output was discarded.") from None
        except UnicodeError:
            raise ChatGPTClientError("ChatGPT stream was not valid UTF-8.") from None
        finally:
            response.close()
