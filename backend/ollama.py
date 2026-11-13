"""Bounded local Ollama client used for highlight and metadata inference."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from backend.config import settings


class OllamaError(RuntimeError):
    """Raised when local Ollama cannot complete a requested inference."""


def available_models() -> list[str]:
    try:
        with httpx.Client(timeout=5) as client:
            response = client.get(f"{settings.ollama_url}/api/tags")
            response.raise_for_status()
            return [str(model.get("name", "")) for model in response.json().get("models", [])]
    except (httpx.HTTPError, ValueError) as exc:
        raise OllamaError(
            f"Local Ollama is not reachable at {settings.ollama_url}. Start Ollama before using AI features."
        ) from exc


def health() -> dict[str, Any]:
    try:
        models = available_models()
        return {"available": True, "models": models, "url": settings.ollama_url}
    except OllamaError as exc:
        return {"available": False, "models": [], "url": settings.ollama_url, "error": str(exc)}


def _extract_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opening, closing in (("{", "}"), ("[", "]")):
        start = cleaned.find(opening)
        end = cleaned.rfind(closing)
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise OllamaError("Ollama returned text that was not valid JSON.")


def chat_json(
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    num_predict: int = 1800,
    temperature: float = 0.2,
) -> Any:
    """Run one bounded local chat request and return parsed JSON.

    The same request is retried once with a stricter instruction when the model
    returns malformed JSON. No request ever leaves localhost.
    """
    if not model:
        raise OllamaError("Choose an installed Ollama model in Settings.")
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": temperature,
            "num_ctx": 8192,
            "num_predict": num_predict,
            "repeat_penalty": 1.05,
        },
        "keep_alive": "5m",
    }
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=settings.ollama_request_timeout) as client:
                response = client.post(f"{settings.ollama_url}/api/chat", json=payload)
                if response.status_code == 404:
                    raise OllamaError(
                        f"Ollama model '{model}' is not installed locally. Choose an installed model in Settings."
                    )
                response.raise_for_status()
                content = response.json().get("message", {}).get("content", "")
                return _extract_json(content)
        except OllamaError:
            raise
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            last_error = exc
            if attempt == 0:
                messages[-1]["content"] = (
                    prompt
                    + "\n\nYour previous response was invalid. Return one valid JSON object only. "
                    "Do not use markdown fences or commentary."
                )
                payload["messages"] = messages
                continue
    raise OllamaError(f"Local Ollama inference failed: {last_error}")
