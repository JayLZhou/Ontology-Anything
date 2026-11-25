import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ontology_anything.cli import main
from ontology_anything.llm import CachedLLM, LLMError, OpenAICompatLLM, RecordingLLM, ReplayLLM


class _Handler(BaseHTTPRequestHandler):
    seen: list = []
    reject_json_mode = False
    fail_first = 0  # answer this many requests with `fail_status` before succeeding
    fail_status = 429

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.headers["Authorization"], body))
        if type(self).fail_first > 0:
            type(self).fail_first -= 1
            self.send_response(self.fail_status)
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b'{"error": "slow down"}')
            return
        if self.reject_json_mode and "response_format" in body:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "response_format not supported"}')
            return
        reply = {"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    _Handler.seen = []
    _Handler.reject_json_mode = False
    _Handler.fail_first = 0
    _Handler.fail_status = 429
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


def test_openai_compatible_call_and_record(server, tmp_path):
    llm = OpenAICompatLLM(model="m", base_url=server, api_key="k")
    cassette = tmp_path / "c.json"
    rec = RecordingLLM(llm, cassette)
    assert '"ok": true' in rec.complete("enrich", "sys", "user")
    auth, body = _Handler.seen[0]
    assert auth == "Bearer k" and body["model"] == "m" and body["messages"][1]["content"] == "user"
    assert json.loads(cassette.read_text()) == {"enrich": {"ok": True}}
    assert ReplayLLM(cassette).complete("enrich", "", "") == '{"ok": true}'


def test_falls_back_when_json_mode_unsupported(server):
    _Handler.reject_json_mode = True
    llm = OpenAICompatLLM(model="m", base_url=server, api_key="k")
    assert "ok" in llm.complete("k", "s", "u")
    assert llm.json_mode is False


def test_missing_key(monkeypatch):
    monkeypatch.delenv("OA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMError):
        OpenAICompatLLM()


def test_rate_limits_and_server_errors_are_retried(server):
    _Handler.fail_first = 2
    llm = OpenAICompatLLM(model="m", base_url=server, api_key="k", backoff=0)
    assert "ok" in llm.complete("k", "s", "u")
    assert len(_Handler.seen) == 3

    _Handler.fail_first, _Handler.fail_status = 10, 503
    with pytest.raises(LLMError, match="gave up after 1 retries"):
        OpenAICompatLLM(model="m", base_url=server, api_key="k", backoff=0, retries=1).complete("k", "s", "u")


def test_client_errors_are_not_retried(server):
    _Handler.fail_first, _Handler.fail_status = 1, 401
    with pytest.raises(LLMError, match="HTTP 401"):
        OpenAICompatLLM(model="m", base_url=server, api_key="k", backoff=0).complete("k", "s", "u")
    assert len(_Handler.seen) == 1


class _Counting:
    identity = "model-a"

    def __init__(self, answer='{"ok": true}'):
        self.answer, self.calls = answer, 0

    def complete(self, key, system, user):
        self.calls += 1
        return self.answer


def test_cache_answers_repeated_prompts_only(tmp_path):
    inner = _Counting()
    cached = CachedLLM(inner, tmp_path / "cache")
    assert cached.complete("a", "sys", "one") == cached.complete("b", "sys", "one") == '{"ok": true}'
    cached.complete("a", "sys", "two")
    assert (inner.calls, cached.hits, cached.misses) == (2, 1, 2)
    # a fresh process sees the same cache; a different model does not share it
    assert CachedLLM(_Counting("never"), tmp_path / "cache").complete("a", "sys", "one") == '{"ok": true}'
    other = _Counting()
    other.identity = "model-b"
    CachedLLM(other, tmp_path / "cache").complete("a", "sys", "one")
    assert other.calls == 1


def test_cache_does_not_keep_unusable_answers(tmp_path):
    inner = _Counting("I am sorry, I cannot do that")
    cached = CachedLLM(inner, tmp_path)
    cached.complete("a", "s", "u")
    cached.complete("a", "s", "u")
    assert inner.calls == 2 and cached.hits == 0


def test_cli_second_build_makes_no_new_calls(server, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OA_API_KEY", "k")
    monkeypatch.setenv("OA_BASE_URL", server)
    monkeypatch.chdir(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nOrders over 500 must be approved.", encoding="utf-8")
    (docs / "b.md").write_text("# B\n\nRefunds require a receipt.", encoding="utf-8")
    args = ["build", str(docs), "-o", str(tmp_path / "out"), "--workers", "2"]
    assert main(args) == 0
    first = len(_Handler.seen)
    assert first == 3 and "model calls: 3 new, 0 answered" in capsys.readouterr().out  # enrich + two chunks
    assert main(args) == 0
    assert len(_Handler.seen) == first and "model calls: 0 new, 3 answered" in capsys.readouterr().out
    (docs / "b.md").write_text("# B\n\nRefunds require a receipt and a reason.", encoding="utf-8")
    assert main(args) == 0  # the corpus excerpt changed, so enrich is asked again; a.md is not
    assert len(_Handler.seen) == first + 2
    assert main(args + ["--no-cache"]) == 0
    assert len(_Handler.seen) == first + 5
