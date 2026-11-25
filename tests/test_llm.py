import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ontology_anything.llm import LLMError, OpenAICompatLLM, RecordingLLM, ReplayLLM


class _Handler(BaseHTTPRequestHandler):
    seen: list = []
    reject_json_mode = False

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.headers["Authorization"], body))
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
