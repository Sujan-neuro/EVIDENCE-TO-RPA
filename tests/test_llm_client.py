import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))
from llm_client import MockLLMClient, WorkspaceClient, extract_json  # noqa: E402


@pytest.mark.parametrize("reply,expected", [
    # already clean
    ('{"steps": []}', {"steps": []}),
    # fenced, which qwen models emit by default
    ('```json\n{"steps": [1]}\n```', {"steps": [1]}),
    ('```\n{"steps": [2]}\n```', {"steps": [2]}),
    # reasoning-model preamble
    ('<think>weighing the options</think>\n{"steps": [3]}', {"steps": [3]}),
    ('<think>a</think>```json\n{"steps": [4]}\n```', {"steps": [4]}),
    # prose either side of the document
    ('Here you go:\n{"steps": [5]}\nHope that helps.', {"steps": [5]}),
    # a top-level array
    ('```json\n[{"a": 1}]\n```', [{"a": 1}]),
    # braces inside string values must not confuse the span scan
    ('{"note": "use {curly} braces"}', {"note": "use {curly} braces"}),
])
def test_extract_json_unwraps_model_envelopes(reply, expected):
    assert json.loads(extract_json(reply)) == expected


def test_extract_json_passes_malformed_replies_through_unchanged():
    """A genuinely broken reply must reach the caller's retry loop intact, so
    the error message shows what the model actually said."""
    broken = "I could not answer that."
    assert extract_json(broken) == broken
    with pytest.raises(json.JSONDecodeError):
        json.loads(extract_json(broken))


def test_extract_json_handles_empty_reply():
    assert extract_json("") == ""


#: Not a real endpoint. The client is never allowed to reach the network in
#: these tests; this only satisfies the configuration it now insists on.
FAKE_ENDPOINT = "https://example.invalid/compatible-mode/v1"


def test_workspace_client_requires_an_endpoint(monkeypatch):
    """The endpoint is infrastructure, so it is configured, never defaulted --
    a repository that ships one publishes where somebody's workspace lives."""
    monkeypatch.setenv("API_KEY", "test-key-not-real")
    monkeypatch.setenv("LLM_BASE_URL", FAKE_ENDPOINT)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="LLM_BASE_URL is not set"):
        WorkspaceClient()


def test_workspace_client_requires_a_key(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="API_KEY is not set"):
        WorkspaceClient()


def test_workspace_client_reads_config_from_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key-not-real")
    monkeypatch.setenv("LLM_BASE_URL", FAKE_ENDPOINT)
    monkeypatch.setenv("LLM_MODEL", "qwen3-vl-plus")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    client = WorkspaceClient()
    assert client.model == "qwen3-vl-plus"


def test_workspace_client_strips_fences_in_json_mode(monkeypatch):
    """complete() must hand the pipeline something json.loads can take."""
    monkeypatch.setenv("API_KEY", "test-key-not-real")
    monkeypatch.setenv("LLM_BASE_URL", FAKE_ENDPOINT)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    client = WorkspaceClient()
    monkeypatch.setattr(client, "_post", lambda path, payload: {
        "choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]})

    assert json.loads(client.complete([{"role": "user", "content": "hi"}])) == {"ok": True}
    # json_mode=False leaves the reply exactly as the model sent it
    raw = client.complete([{"role": "user", "content": "hi"}], json_mode=False)
    assert raw == '```json\n{"ok": true}\n```'


def test_workspace_client_never_sends_response_format(monkeypatch):
    """Support for response_format varies by model on this endpoint; a 400 from
    an unsupported parameter is worse than a fenced reply we can already strip."""
    monkeypatch.setenv("API_KEY", "test-key-not-real")
    monkeypatch.setenv("LLM_BASE_URL", FAKE_ENDPOINT)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    client = WorkspaceClient()
    seen = {}
    monkeypatch.setattr(client, "_post", lambda path, payload: (
        seen.update(payload) or {"choices": [{"message": {"content": "{}"}}]}))
    client.complete([{"role": "user", "content": "hi"}])
    assert "response_format" not in seen
    assert seen["temperature"] == 0.0


def test_mock_client_still_satisfies_the_interface():
    client = MockLLMClient(['{"a": 1}'])
    assert client.complete([]) == '{"a": 1}'
    assert client.call_count == 1
