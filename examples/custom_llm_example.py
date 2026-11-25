"""Plug in your own model client.

Anything with ``complete(key, system, user) -> str`` works as the model: an
SDK client, a local model, a rate-limited or cached wrapper. ``key`` is a
stable name for the call (``enrich``, ``extract:<doc>#<chunk>``) that you can
use for caching and logging.

This example wraps a client with logging and an on-disk cache. To stay
runnable without an API key it wraps the recorded answers; swap ``inner`` for
your own client.

    python examples/custom_llm_example.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ontology_anything import build, load_corpus
from ontology_anything.llm import LLM, ReplayLLM

HERE = Path(__file__).resolve().parent / "procurement"


class CachedLLM:
    """Answer from ``cache_dir`` when possible, otherwise ask ``inner`` and remember."""

    def __init__(self, inner: LLM, cache_dir: Path):
        self.inner = inner
        self.cache_dir = cache_dir
        self.calls = 0
        cache_dir.mkdir(parents=True, exist_ok=True)

    def complete(self, key: str, system: str, user: str) -> str:
        cached = self.cache_dir / (key.replace("/", "_").replace(":", "_") + ".json")
        if cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))["answer"]
        self.calls += 1
        print(f"  calling the model for {key} ({len(system) + len(user)} prompt characters)")
        answer = self.inner.complete(key, system, user)
        cached.write_text(json.dumps({"answer": answer}, ensure_ascii=False), encoding="utf-8")
        return answer


def main() -> None:
    corpus = load_corpus([HERE / "docs", HERE / "data"])
    inner = ReplayLLM(HERE / "cassette.json")  # replace with your own client
    with tempfile.TemporaryDirectory() as tmp:
        llm = CachedLLM(inner, Path(tmp))
        print("first build:")
        first = build(corpus, llm, name="procurement", today="2026-11-24")
        print(f"  {llm.calls} model calls")
        llm.calls = 0
        print("second build (same documents):")
        second = build(corpus, llm, name="procurement", today="2026-11-24")
        print(f"  {llm.calls} model calls")
    assert first.to_dict() == second.to_dict()
    print(f"both drafts are identical: {len(first.ontology['action_types'])} actions, "
          f"{sum(len(a['rules']) for a in first.ontology['action_types'].values())} rules")


if __name__ == "__main__":
    main()
