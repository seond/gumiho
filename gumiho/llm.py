"""Local LLM access. Ollama now; the interface stays provider-agnostic
so an MLX backend can slot in later.
"""

import json
import re

import aiohttp

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LlmError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host: str = "http://127.0.0.1:11434",
                 model: str = "qwen3:8b", timeout: float = 90.0) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def chat(self, system: str, user: str, temperature: float = 0.3) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "think": False,          # qwen3: suppress the reasoning preamble
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": temperature},
        }
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as http:
                async with http.post(f"{self.host}/api/chat", json=payload) as resp:
                    if resp.status != 200:
                        raise LlmError(f"ollama HTTP {resp.status}: {await resp.text()}")
                    data = await resp.json()
        except aiohttp.ClientError as e:
            raise LlmError(f"ollama unreachable: {e}") from e
        content = data.get("message", {}).get("content", "")
        return _THINK_RE.sub("", content).strip()


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise LlmError(f"no JSON in reply: {text[:200]!r}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LlmError(f"bad JSON in reply: {e}: {m.group(0)[:200]!r}") from e
