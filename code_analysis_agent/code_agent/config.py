"""에이전트 설정 로딩.

우선순위: 기본값 < 설정 파일(JSON) < 환경변수 < CLI 인자
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_CONFIG_PATHS = [
    Path("code_agent.json"),
    Path.home() / ".code_agent.json",
]

ENV_MAP = {
    "CODE_AGENT_PROVIDER": "provider",
    "CODE_AGENT_BASE_URL": "base_url",
    "CODE_AGENT_MODEL": "model",
    "CODE_AGENT_API_KEY": "api_key",
    "CODE_AGENT_CONTEXT_CHARS": "context_chars",
    "CODE_AGENT_TIMEOUT": "timeout",
}


@dataclass
class AgentConfig:
    # --- LLM 연결 (사내 로컬 모델) ---
    # provider: "openai" = OpenAI 호환 API(vLLM, TGI, LM Studio, LiteLLM 등)
    #           "ollama" = Ollama 네이티브 /api/chat
    provider: str = "openai"
    base_url: str = "http://localhost:8000/v1"
    model: str = "local-model"
    api_key: str = ""
    temperature: float = 0.1
    max_tokens: int = 2048
    timeout: int = 300
    retries: int = 2
    verify_ssl: bool = True
    ca_bundle: str = ""
    extra_headers: Dict[str, str] = field(default_factory=dict)

    # --- 컨텍스트 예산 ---
    # 모델에 한 번에 넣을 코드 컨텍스트 문자 수(대략 토큰 x 3~4).
    # 이 값을 넘으면 map-reduce(부분 분석 후 종합)로 처리한다.
    context_chars: int = 24000
    max_map_batches: int = 8

    # --- 인덱싱 ---
    include_ext: List[str] = field(default_factory=lambda: [
        ".py", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
        ".java", ".cs", ".js", ".jsx", ".ts", ".tsx", ".go", ".kt", ".rs",
        ".m", ".mm", ".swift", ".scala",
    ])
    exclude_dirs: List[str] = field(default_factory=lambda: [
        ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
        "build", "dist", "out", "bin", "obj", ".idea", ".vs", ".vscode",
        "third_party", "external",
    ])
    max_file_kb: int = 512

    # --- 분석 ---
    expand_depth: int = 1        # 호출자/피호출자 확장 깊이
    max_related: int = 12        # 확장으로 추가할 관련 심볼 최대 개수
    max_keyword_hits: int = 40   # 키워드 검색 최대 히트 수
    language: str = "ko"         # 답변 언어

    @classmethod
    def load(cls, path: Optional[str] = None, **overrides) -> "AgentConfig":
        cfg = cls()
        candidates = [Path(path)] if path else DEFAULT_CONFIG_PATHS
        for p in candidates:
            if p.is_file():
                cfg._apply(json.loads(p.read_text(encoding="utf-8")))
                break
            if path:
                raise FileNotFoundError(f"설정 파일을 찾을 수 없음: {path}")
        cfg._apply({v: os.environ[k] for k, v in ENV_MAP.items() if os.environ.get(k)})
        cfg._apply({k: v for k, v in overrides.items() if v is not None})
        return cfg

    def _apply(self, data: dict) -> None:
        known = {f.name: f for f in fields(self)}
        for key, value in data.items():
            if key not in known:
                continue
            current = getattr(self, key)
            if isinstance(current, bool) and isinstance(value, str):
                value = value.lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int) and not isinstance(current, bool) and isinstance(value, str):
                value = int(value)
            elif isinstance(current, float) and isinstance(value, str):
                value = float(value)
            setattr(self, key, value)
