"""LLM clients.

Every call carries a stable ``key`` (e.g. ``actions:policy.md#0``) as well as
the prompt. That makes runs recordable and replayable: record once against a
real model, then replay deterministically in tests, demos and CI.

* ``openai``             -- any OpenAI-compatible endpoint (OpenAI, DeepSeek, Qwen,
                            vLLM, Ollama ...): set OA_API_KEY / OA_BASE_URL / OA_MODEL
* ``replay:<file.json>`` -- answer from a recorded cassette
* ``record:<file.json>`` -- call the real model and save answers to a cassette

:class:`CachedLLM` wraps any of them with an on-disk cache keyed by the prompt,
so an interrupted or repeated build only pays for what changed. All clients
may be called from several threads at once.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
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
        retries: int = 3,
        backoff: float = 2.0,
    ):
        self.model = model or os.environ.get("OA_MODEL", "gpt-4o-mini")
        self.base_url = (base_url or os.environ.get("OA_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("OA_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.temperature = temperature
        self.timeout = timeout
        self.json_mode = json_mode
        self.retries = retries
        self.backoff = backoff
        if not self.api_key:
            raise LLMError("set OA_API_KEY (or OPENAI_API_KEY) to use a real model, or use --llm replay:<cassette>")

    @property
    def identity(self) -> str:
        """What a cached answer depends on besides the prompt."""
        return f"{self.base_url}|{self.model}|{self.temperature}"

    def complete(self, key: str, system: str, user: str) -> str:
        attempt = 0
        while True:
            try:
                return self._complete_once(key, system, user)
            except _Retryable as exc:
                attempt += 1
                if attempt > self.retries:
                    raise LLMError(f"{key}: {exc} (gave up after {self.retries} retries)") from None
                wait = exc.retry_after if exc.retry_after is not None else self.backoff * 2 ** (attempt - 1)
                time.sleep(min(wait, 60.0))

    def _complete_once(self, key: str, system: str, user: str) -> str:
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
                return self._complete_once(key, system, user)
            if exc.code in (408, 409, 429) or exc.code >= 500:  # rate limits and transient server errors
                raise _Retryable(f"HTTP {exc.code}: {detail}", _retry_after(exc.headers.get("Retry-After"))) from None
            raise LLMError(f"{key}: HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise _Retryable(str(getattr(exc, "reason", exc))) from None
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"{key}: unexpected response {str(payload)[:300]}") from None


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


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
        self._lock = threading.Lock()

    def complete(self, key: str, system: str, user: str) -> str:
        answer = self.inner.complete(key, system, user)
        try:
            parsed = parse_json(answer)
        except LLMError:
            parsed = answer
        with self._lock:
            self.cassette[key] = parsed
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps(self.cassette, ensure_ascii=False, indent=2))
        return answer


class CachedLLM:
    """Remember answers on disk, keyed by the model and the full prompt.

    Rebuilding after an interruption, or after editing one document out of
    many, then only calls the model for prompts it has not answered before.
    Answers that are not valid JSON are not cached, so a bad reply is retried
    on the next run.
    """

    def __init__(self, inner: LLM, cache_dir: str | Path, namespace: str | None = None):
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self.namespace = namespace if namespace is not None else getattr(inner, "identity", type(inner).__name__)
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def _path(self, system: str, user: str) -> Path:
        digest = hashlib.sha256("\x00".join((self.namespace, system, user)).encode("utf-8")).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.json"

    def complete(self, key: str, system: str, user: str) -> str:
        path = self._path(system, user)
        try:
            answer = json.loads(path.read_text(encoding="utf-8"))["answer"]
        except (OSError, ValueError, KeyError, TypeError):
            answer = None
        if isinstance(answer, str):
            with self._lock:
                self.hits += 1
            return answer
        answer = self.inner.complete(key, system, user)
        with self._lock:
            self.misses += 1
        try:
            parse_json(answer)
        except LLMError:
            return answer
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, json.dumps({"key": key, "answer": answer}, ensure_ascii=False))
        return answer


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def from_spec(spec: str, model: str | None = None, cache_dir: str | Path | None = None) -> LLM:
    """``cache_dir`` adds an on-disk cache in front of a real model (never in front of a replay)."""
    def real() -> LLM:
        client = OpenAICompatLLM(model=model)
        return CachedLLM(client, cache_dir) if cache_dir else client

    if spec.startswith("replay:"):
        return ReplayLLM(spec.split(":", 1)[1])
    if spec.startswith("record:"):
        return RecordingLLM(real(), spec.split(":", 1)[1])
    if spec == "openai":
        return real()
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
