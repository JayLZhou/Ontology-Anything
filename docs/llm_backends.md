# Models: providers, record / replay, custom clients

Ontology-Anything asks a model for exactly two kinds of answer -- one `enrich` call for the whole corpus, and one `extract` call per document chunk -- and treats both as untrusted. This page covers how to point it at a model, how to make runs reproducible, and how to bring your own client.

## Providers

Any endpoint that speaks the OpenAI chat-completions protocol works. Configuration is three environment variables, read from the shell or from a `.env` file in the folder you run the command in (see [`.env.example`](../.env.example)):

| Variable | Default | Meaning |
|---|---|---|
| `OA_API_KEY` | falls back to `OPENAI_API_KEY` | API key |
| `OA_BASE_URL` | `https://api.openai.com/v1` | endpoint; the client posts to `<base>/chat/completions` |
| `OA_MODEL` | `gpt-4o-mini` | model name; `--model` on the command line overrides it |

| Provider | `OA_BASE_URL` |
|---|---|
| OpenAI | *(default)* |
| DeepSeek | `https://api.deepseek.com/v1` |
| Qwen (DashScope) | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Ollama | `http://localhost:11434/v1` |
| vLLM / LM Studio | `http://localhost:8000/v1` / `http://localhost:1234/v1` |

Local servers usually ignore the key, but one must be set: use any placeholder such as `OA_API_KEY=local`.

Requests use temperature 0 and JSON mode (`response_format: json_object`). If the endpoint rejects JSON mode with HTTP 400, the client retries without it and keeps it off for the rest of the run; answers wrapped in code fences or surrounded by prose are still parsed.

## Long corpora: workers, cache, retries

```bash
ontology-anything build ./docs ./data -o out --workers 8
```

**Workers.** Extraction calls are sent in batches of `--workers` (default 1, or `OA_WORKERS`). All calls of a batch are given the same list of actions found so far, and their answers are merged in document order, so the result does not depend on which call returns first. The trade-off is visibility: within a batch, one chunk cannot reuse an action name another chunk is defining at the same moment. With one worker every chunk sees everything before it.

**Cache.** With a real model, answers are stored under `<out>/.llm-cache` (`--cache-dir` to move it, `--no-cache` to disable), one file per prompt, keyed by a hash of the endpoint, model, temperature and the complete prompt. Consequences:

- An interrupted build resumes where it stopped when you run the same command again.
- Editing one document re-asks about its own chunks, and about any other chunk whose prompt changes as a result. Prompts contain the schema, the roles and the actions found so far, so an edit that adds a role or renames an action reaches further than one that rewords a sentence. The single `enrich` call is repeated when the edit falls within the first 2,000 characters of a document.
- Changing the model, the endpoint or the prompts invalidates the cache by construction.
- Replies that are not valid JSON are never cached, so they are retried next time.

The build prints how many calls were new and how many came from the cache. The cache sits *in front of* the model and behind recording: `--llm record:<file>` still writes every answer to the cassette. Replays do not use it.

**Retries.** HTTP 408, 409, 429 and 5xx responses, timeouts and connection errors are retried three times with exponential backoff (2 s, 4 s, 8 s), or after the server's `Retry-After`. Other errors, such as 401, fail immediately. A call that still fails becomes a note on the draft and the build continues with the remaining chunks.

In Python:

```python
from ontology_anything.llm import CachedLLM, OpenAICompatLLM

llm = CachedLLM(OpenAICompatLLM(retries=5), "out/.llm-cache")
draft = build(corpus, llm, workers=8, skip_irrelevant=True)
print(llm.misses, "new calls,", llm.hits, "from cache")
```

## Record and replay

Every call carries a stable **key** -- `enrich`, or `extract:<document id>#<chunk index>` -- and a *cassette* is a JSON file mapping keys to answers:

```json
{
  "enrich": {"object_types": {}, "new_object_types": {}, "roles": []},
  "extract:docs/policy.md#0": {"actions": [], "rules": [], "facts": [], "roles": []}
}
```

```bash
# call the model once and keep its answers
ontology-anything build ./docs ./data -o out --llm record:out/cassette.json

# rebuild from the same answers: no network, no cost, same result every time
ontology-anything build ./docs ./data -o out --llm replay:out/cassette.json
```

What this is good for:

- **Iterating on review decisions** without paying for extraction again.
- **Tests and CI**: this repository's test suite and demo run entirely from [`examples/procurement/cassette.json`](../examples/procurement/cassette.json).
- **Bug reports**: a cassette plus the input files reproduces a run exactly, without sharing a key.
- **Hand-editing**: fix or add an answer in the cassette and replay to see how the pipeline reacts. The demo cassette contains one hallucinated quote and one rule on a non-existent field for exactly this reason.

Keys depend on document ids, which are paths relative to the common root of the inputs. Replaying against renamed or moved files reports the missing key in the draft's notes instead of failing.

Recording into an existing cassette updates the keys that were called and keeps the rest.

## Custom clients

The model is any object with one method:

```python
class MyLLM:
    def complete(self, key: str, system: str, user: str) -> str:
        """Return the model's reply as text. It should be one JSON object."""
```

```python
from ontology_anything import build, load_corpus

draft = build(load_corpus(["./docs", "./data"]), MyLLM(), name="procurement")
```

Raise `ontology_anything.llm.LLMError` for failures you want recorded as a note on the draft (the build continues with the remaining chunks); any other exception propagates. [`examples/custom_llm_example.py`](../examples/custom_llm_example.py) wraps a client with logging and an on-disk cache.

Passing `None` as the model, or `--llm none`, runs only the deterministic table stage.

## What the model is asked for

The prompts live in [`ontology_anything/prompts.py`](../ontology_anything/prompts.py). Both include the schema inferred from the tables, so the model writes rules against real property names:

- **enrich** -- human labels for the inferred types and properties, object types the documents describe but the tables do not cover, and roles.
- **extract** -- per chunk: actions (parameters, allowed roles, effects), rules (`when` expression, outcome `deny` / `warn` / `require_approval`, message, effective date), facts (changes to specific records) and roles. Every item must carry the verbatim sentence it relies on.

Nothing the model returns is used before it passes quote verification and compilation; see [Review and approval](review_and_approval.md).
