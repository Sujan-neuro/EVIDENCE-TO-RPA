"""Provider-agnostic LLM client interface.

Every stage that calls a model -- IR synthesis and Robot composition --
depends only on `LLMClient.complete`, never on a vendor SDK.
That keeps the whole pipeline testable without an API key via MockLLMClient,
and makes swapping models an env var rather than a code change.
"""
import json
import os
import re
from abc import ABC, abstractmethod


class LLMClient(ABC):
    @abstractmethod
    def complete(self, messages: list[dict], *, json_mode: bool = True) -> str:
        """Returns the raw text content of the model's reply."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# reply cleanup
# --------------------------------------------------------------------------
#
# Both pipeline stages json.loads() whatever complete() returns, and retry on
# failure. Models reached through this endpoint routinely wrap their JSON in a
# ```json fence, and reasoning models emit a <think> block ahead of the answer.
# Handing that straight back would burn a retry attempt on a reply that was
# actually correct, so the envelope is stripped here rather than being made
# every caller's problem.

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


def extract_json(text: str) -> str:
    """Return the JSON document embedded in a model reply, as text.

    Falls back to returning the input unchanged when nothing JSON-shaped is
    found, so that a genuinely malformed reply still reaches the caller's
    retry loop with its original content intact for the error message.
    """
    if not text:
        return text
    cleaned = _THINK_RE.sub("", text).strip()

    fenced = _FENCE_RE.search(cleaned)
    if fenced:
        candidate = fenced.group(1).strip()
        if _parses(candidate):
            return candidate

    if _parses(cleaned):
        return cleaned

    # Otherwise take the outermost {...} or [...] span and hope it is the
    # whole document; prose before or after the JSON is the common case.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            candidate = cleaned[start:end + 1]
            if _parses(candidate):
                return candidate
    return cleaned


def _parses(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


# --------------------------------------------------------------------------
# real client
# --------------------------------------------------------------------------

class WorkspaceClient(LLMClient):
    """An OpenAI-compatible chat endpoint, reached with a plain POST.

    No vendor SDK is pinned: the endpoint speaks the /chat/completions shape,
    so `requests` is enough, and the client cannot break on an SDK major
    version bump. `response_format` is deliberately NOT sent -- support for it
    varies across models on this endpoint, and a 400 from an unsupported
    parameter is a worse failure than a fenced reply that `extract_json`
    already handles.
    """

    #: No default. The endpoint is infrastructure, not configuration: hard-coding
    #: one publishes where a particular workspace lives, and anyone running this
    #: against their own provider has to edit source to change it. Set
    #: LLM_BASE_URL in the environment, or pass base_url=.
    DEFAULT_BASE_URL = None
    #: Measured against one provider, not assumed, and recorded because the
    #: lesson generalises: a `GET /models` listing advertises far more than an
    #: account is usually entitled to call. Of 100 text models probed there,
    #: 97 returned 403. Treat a published list as advertising, not inventory,
    #: and confirm what you can actually call before designing around it.
    #:
    #: Benchmarked on the real evidence->IR prompt (price-excess.json), which
    #: is the only comparison that matters here:
    #:
    #:     qwen3.6-flash    67.4s   1 attempt    valid IR
    #:     qwen3-vl-plus   208.0s   3 attempts   exhausted retries
    #:     qwen3.8-max     900.4s   0 attempts   read timeout, never returned
    #:
    #: qwen3.8-max is a reasoning model and cannot finish this prompt inside
    #: 15 minutes, so it is unusable despite reading structure most carefully.
    DEFAULT_MODEL = "qwen3.6-flash"
    CALLABLE_MODELS = ("qwen3.6-flash", "qwen3-vl-plus", "qwen3.8-max")
    FALLBACK_MODELS = ("qwen3-vl-plus", "qwen3.8-max")

    def __init__(self, model: str | None = None, *, base_url: str | None = None,
                 api_key: str | None = None, timeout: float = 900.0,
                 max_tokens: int = 16000, temperature: float = 0.0):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        self._api_key = api_key or os.environ.get("API_KEY") or os.environ.get("LLM_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "API_KEY is not set. Copy .env.example to .env and fill it in, "
                "or export API_KEY in your shell before running the pipeline."
            )
        endpoint = base_url or os.environ.get("LLM_BASE_URL") or self.DEFAULT_BASE_URL
        if not endpoint:
            raise RuntimeError(
                "LLM_BASE_URL is not set. This is the OpenAI-compatible endpoint the "
                "pipeline POSTs to, e.g. https://<host>/compatible-mode/v1 . Copy "
                ".env.example to .env and fill it in, or export LLM_BASE_URL.")
        self._base_url = endpoint.rstrip("/")
        self._model = model or os.environ.get("LLM_MODEL", self.DEFAULT_MODEL)
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._temperature = temperature

    @property
    def model(self) -> str:
        return self._model

    def _post(self, path: str, payload: dict) -> dict:
        import requests
        try:
            response = requests.post(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=self._timeout)
        except Exception as exc:
            raise RuntimeError(
                f"LLM request failed: {type(exc).__name__}: {exc}") from exc
        if not response.ok:
            # The key must never reach a log or a run artifact.
            raise RuntimeError(
                f"LLM endpoint returned {response.status_code}: "
                f"{response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"LLM reply was not JSON: {response.text[:300]}") from exc

    def complete(self, messages: list[dict], *, json_mode: bool = True) -> str:
        payload = self._post("/chat/completions", {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        })
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"unexpected LLM response shape: {json.dumps(payload)[:300]}") from exc
        return extract_json(content) if json_mode else content

    def models(self) -> list[str]:
        """Every model id the endpoint offers. Useful for checking access."""
        import requests
        try:
            response = requests.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout)
        except Exception as exc:
            raise RuntimeError(
                f"LLM request failed: {type(exc).__name__}: {exc}") from exc
        if not response.ok:
            raise RuntimeError(f"{response.status_code}: {response.text[:300]}")
        return [m.get("id") for m in response.json().get("data", [])]


#: Backwards-compatible alias. The pipeline previously constructed an
#: `OpenAIClient`; the endpoint is OpenAI-compatible, so the name still reads
#: true and existing imports keep working.
OpenAIClient = WorkspaceClient


class MockLLMClient(LLMClient):
    """Returns a fixed sequence of canned responses, one per call, so the
    retry-on-validation-failure loop can be exercised deterministically
    without any network access or API key. Used by tests and for offline
    development of the pipeline plumbing.
    """

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._call_count = 0

    def complete(self, messages: list[dict], *, json_mode: bool = True) -> str:
        if self._call_count >= len(self._responses):
            raise RuntimeError("MockLLMClient ran out of canned responses")
        response = self._responses[self._call_count]
        self._call_count += 1
        return response

    @property
    def call_count(self) -> int:
        return self._call_count


def main() -> int:
    """Check endpoint access: `python pipeline/llm_client.py [--models]`."""
    import sys
    client = WorkspaceClient()
    if "--models" in sys.argv:
        for m in client.models():
            print(m)
        return 0
    reply = client.complete(
        [{"role": "user", "content": 'Reply with exactly {"ok": true}'}])
    print(f"model  : {client.model}")
    print(f"reply  : {reply}")
    print("parsed :", json.loads(reply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
