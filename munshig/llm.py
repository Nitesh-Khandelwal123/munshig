"""Local AI backends. Nothing here calls a cloud service.

- OllamaLLM talks to the Ollama server OpenJarvis installs (localhost:11434).
- OpenJarvisLLM routes text prompts through an installed OpenJarvis. Image
  reading still goes straight to Ollama, because that is where the vision
  model runs.
- ScriptedLLM returns canned answers, for tests and the offline demo.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import httpx

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(Exception):
    pass


class LLM(Protocol):
    def complete(self, system: str, prompt: str, *, images: list[bytes] | None = None,
                 schema: dict | None = None, vision: bool = False) -> str: ...


def parse_json(text: str) -> dict:
    """Pull the JSON object out of a model reply (tolerates code fences and stray text)."""
    text = _THINK.sub("", text).strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"Model reply had no JSON object: {text[:200]!r}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"Model reply was not valid JSON: {exc}") from exc


@dataclass
class OllamaLLM:
    url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3:4b"
    vision_model: str = "qwen2.5vl:7b"
    timeout: float = 300.0
    transport: httpx.BaseTransport | None = None

    def complete(self, system: str, prompt: str, *, images: list[bytes] | None = None,
                 schema: dict | None = None, vision: bool = False) -> str:
        user: dict = {"role": "user", "content": prompt}
        if images:
            user["images"] = [base64.b64encode(img).decode() for img in images]
        body = {
            "model": self.vision_model if (vision or images) else self.chat_model,
            "messages": [{"role": "system", "content": system}, user],
            "stream": False,
            "options": {"temperature": 0},
        }
        if schema:
            body["format"] = schema
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport, trust_env=False) as client:
                resp = client.post(f"{self.url}/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach Ollama at {self.url}. Is it running? ({exc})") from exc
        if resp.status_code != 200:
            raise LLMError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
        return _THINK.sub("", resp.json().get("message", {}).get("content", "")).strip()

    def available_models(self) -> list[str]:
        with httpx.Client(timeout=10, transport=self.transport, trust_env=False) as client:
            resp = client.get(f"{self.url}/api/tags")
        return [m["name"] for m in resp.json().get("models", [])]


@dataclass
class OpenJarvisLLM:
    """Text prompts through OpenJarvis; images through Ollama directly."""
    ollama: OllamaLLM
    _jarvis: object | None = None

    def _get(self):
        if self._jarvis is None:
            try:
                from openjarvis import Jarvis  # type: ignore
            except ImportError as exc:
                raise LLMError("OpenJarvis isn't installed. Install it, or set [ai] backend = \"ollama\".") from exc
            self._jarvis = Jarvis(model=self.ollama.chat_model, engine_key="ollama")
        return self._jarvis

    def complete(self, system: str, prompt: str, *, images: list[bytes] | None = None,
                 schema: dict | None = None, vision: bool = False) -> str:
        if images or vision:
            return self.ollama.complete(system, prompt, images=images, schema=schema, vision=True)
        if schema:
            prompt += "\n\nAnswer with one JSON object matching this schema:\n" + json.dumps(schema)
        reply = self._get().ask(f"{system}\n\n{prompt}", agent="simple", temperature=0.0)
        return _THINK.sub("", str(reply)).strip()


@dataclass
class ScriptedLLM:
    """Returns answers chosen by a function of the prompt. For tests and the demo."""
    answer: Callable[[str, str, bool], str]
    calls: list[tuple[str, bool]] = field(default_factory=list)

    def complete(self, system: str, prompt: str, *, images: list[bytes] | None = None,
                 schema: dict | None = None, vision: bool = False) -> str:
        used_vision = bool(images) or vision
        self.calls.append((prompt, used_vision))
        return self.answer(system, prompt, used_vision)


def from_settings(ai) -> LLM | None:
    if ai.backend == "none":
        return None
    ollama = OllamaLLM(url=ai.ollama_url, chat_model=ai.chat_model,
                       vision_model=ai.vision_model, timeout=ai.timeout_seconds)
    if ai.backend == "openjarvis":
        return OpenJarvisLLM(ollama)
    if ai.backend == "ollama":
        return ollama
    raise ValueError(f"Unknown [ai] backend {ai.backend!r}; use ollama, openjarvis or none")


def read_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()
