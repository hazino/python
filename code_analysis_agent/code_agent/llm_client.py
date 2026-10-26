"""사내 로컬 LLM 클라이언트 (표준 라이브러리만 사용).

- provider="openai": POST {base_url}/chat/completions  (vLLM, TGI, LM Studio, LiteLLM, sglang ...)
- provider="ollama": POST {base_url}/api/chat
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Dict, List

from .config import AgentConfig

Message = Dict[str, str]


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages: List[Message], max_tokens: int | None = None) -> str:
        cfg = self.config
        if cfg.provider == "ollama":
            url = cfg.base_url.rstrip("/") + "/api/chat"
            payload = {
                "model": cfg.model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": cfg.temperature,
                    "num_predict": max_tokens or cfg.max_tokens,
                },
            }
        elif cfg.provider == "openai":
            url = cfg.base_url.rstrip("/") + "/chat/completions"
            payload = {
                "model": cfg.model,
                "messages": messages,
                "temperature": cfg.temperature,
                "max_tokens": max_tokens or cfg.max_tokens,
                "stream": False,
            }
        else:
            raise LLMError(f"지원하지 않는 provider: {cfg.provider}")

        data = self._post(url, payload)
        try:
            if cfg.provider == "ollama":
                return data["message"]["content"]
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"예상치 못한 응답 형식: {json.dumps(data)[:500]}") from e

    def _post(self, url: str, payload: dict) -> dict:
        cfg = self.config
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        headers.update(cfg.extra_headers)

        context = None
        if url.startswith("https"):
            if not cfg.verify_ssl:
                # 사내 자체 서명 인증서 환경용 (가능하면 ca_bundle 사용 권장)
                context = ssl._create_unverified_context()
            elif cfg.ca_bundle:
                context = ssl.create_default_context(cafile=cfg.ca_bundle)

        body = json.dumps(payload).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(cfg.retries + 1):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=cfg.timeout, context=context) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                last_err = LLMError(f"HTTP {e.code} from {url}: {detail}")
                if e.code < 500 and e.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = LLMError(f"{url} 연결 실패: {e}")
            if attempt < cfg.retries:
                time.sleep(2 ** attempt)
        raise last_err  # type: ignore[misc]
