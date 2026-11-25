"""LLM clients.

Every call carries a stable ``key`` (e.g. ``actions:policy.md#0``) as well as
the prompt. That makes runs recordable and replayable: record once against a
real model, then replay deterministically in tests, demos and CI.

* ``openai``             -- any OpenAI-compatible endpoint (OpenAI, DeepSeek, Qwen,
                            vLLM, Ollama ...): set OA_API_KEY / OA_BASE_URL / OA_MODEL
* ``replay:<file.json>`` -- answer from a recorded cassette
* ``record:<file.json>`` -- call the real model and save answers to a cassette
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def complete(self, key: str, system: str, user: str) -> str: ...


class OpenAICompatLLM:
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        timeout: float = 180.0,
        json_mode: bool = True,
    ):
        self.model = model or os.environ.get("OA_MODEL", "gpt-4o-mini")
        self.base_url = (base_url or os.environ.get("OA_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("OA_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.temperature = temperature
        self.timeout = timeout
        self.json_mode = json_mode
        if not self.api_key:
            raise LLMError("set OA_API_KEY (or OPENAI_API_KEY) to use a real model, or use --llm replay:<cassette>")

    def complete(self, key: str, system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code == 400 and self.json_mode and "response_format" in detail:
                self.json_mode = False  # provider does not support JSON mode; retry without it
                return self.complete(key, system, user)
            raise LLMError(f"{key}: HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise LLMError(f"{key}: {exc.reason}") from None
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"{key}: unexpected response {str(payload)[:300]}") from None


class ReplayLLM:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            self.cassette: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMError(f"cannot read cassette {self.path}: {exc}") from None

    def complete(self, key: str, system: str, user: str) -> str:
        if key not in self.cassette:
            raise LLMError(
                f"cassette {self.path.name} has no answer for {key!r}; record it with --llm record:{self.path}"
            )
        answer = self.cassette[key]
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


class RecordingLLM:
    def __init__(self, inner: LLM, path: str | Path):
        self.inner = inner
        self.path = Path(path)
        self.cassette: dict[str, Any] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        )

    def complete(self, key: str, system: str, user: str) -> str:
        answer = self.inner.complete(key, system, user)
        try:
            self.cassette[key] = parse_json(answer)
        except LLMError:
            self.cassette[key] = answer
        self.path.write_text(json.dumps(self.cassette, ensure_ascii=False, indent=2), encoding="utf-8")
        return answer


def from_spec(spec: str, model: str | None = None) -> LLM:
    if spec.startswith("replay:"):
        return ReplayLLM(spec.split(":", 1)[1])
    if spec.startswith("record:"):
        return RecordingLLM(OpenAICompatLLM(model=model), spec.split(":", 1)[1])
    if spec == "openai":
        return OpenAICompatLLM(model=model)
    raise LLMError(f"unknown --llm {spec!r}; use openai, replay:<file>, record:<file> or none")


def parse_json(text: str) -> Any:
    """Parse model output that should be JSON, tolerating code fences and chatter."""
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"model did not return valid JSON: {text[:200]!r}")
