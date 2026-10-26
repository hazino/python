"""프롬프트에서 '집중 분석 대상'을 추출한다.

지원하는 지정 방식 (섞어서 사용 가능):

  1) 명시 태그      class:Engine  func:Engine::step  method:run  keyword:"timeout"
                    file:src/net/socket.cpp  symbol:Parser
                    (한글 태그: 클래스:, 함수:, 메서드:, 키워드:, 파일:)
  2) 백틱           `Engine::step`  `parse_config()`
  3) 따옴표         "retry count"  → 키워드(문자열) 검색
  4) 한글 문맥      "Engine 클래스", "step 함수", "timeout 키워드"
  5) 식별자 추정    CamelCase, snake_case, Foo::bar, foo(), obj.method
                    → 인덱스에 실제로 존재할 때만 대상이 됨

분석 모드(structure/flow/bug/refactor/explain/general)도 프롬프트 키워드로 추정한다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

KIND_ALIASES = {
    "class": "class", "cls": "class", "struct": "class", "클래스": "class", "구조체": "class",
    "interface": "class", "인터페이스": "class",
    "func": "function", "function": "function", "fn": "function", "def": "function",
    "함수": "function", "method": "function", "메서드": "function", "메소드": "function",
    "symbol": None, "sym": None, "심볼": None,
}
KEYWORD_TAGS = {"keyword", "kw", "grep", "키워드", "검색"}
FILE_TAGS = {"file", "path", "파일", "경로"}

TAG_RE = re.compile(
    r"(?P<tag>[A-Za-z]+|클래스|구조체|인터페이스|함수|메서드|메소드|심볼|키워드|검색|파일|경로)\s*[:=]\s*"
    r"(?:\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)'|`(?P<bq>[^`]+)`|(?P<bare>[^\s,;]+))"
)
BACKTICK_RE = re.compile(r"`([^`]+)`")
QUOTE_RE = re.compile(r"\"([^\"]+)\"|“([^”]+)”|'([^'\s][^']*[^'\s])'")
IDENT = r"~?[A-Za-z_][A-Za-z0-9_]*"
QUALIFIED_RE = re.compile(rf"{IDENT}(?:(?:::|\.|->){IDENT})+(?:\(\))?|{IDENT}\(\)")
CTX_RE = re.compile(
    rf"(?P<name>{IDENT}(?:(?:::|\.){IDENT})*)\s*(?:\(\))?\s*"
    r"(?P<kind>클래스|구조체|인터페이스|함수|메서드|메소드|키워드|class|struct|function|method|keyword)"
    r"|(?P<kind2>class|struct|function|method|keyword)\s+(?P<name2>" + IDENT + r"(?:(?:::|\.)" + IDENT + r")*)"
)
WORD_RE = re.compile(IDENT)

MODE_HINTS = [
    ("structure", ["구조", "아키텍처", "설계", "관계", "상속", "의존", "structure", "architecture",
                   "hierarchy", "dependency", "uml", "다이어그램"]),
    ("flow", ["흐름", "호출", "시퀀스", "동작 순서", "실행 순서", "flow", "call graph", "sequence",
              "어떻게 동작", "처리 과정", "콜 스택"]),
    ("bug", ["버그", "오류", "에러", "문제점", "결함", "크래시", "누수", "race", "bug", "error",
             "crash", "leak", "취약", "deadlock", "데드락"]),
    ("refactor", ["리팩", "개선", "최적화", "성능", "refactor", "improve", "optimiz", "clean",
                  "중복", "가독성"]),
    ("explain", ["설명", "무슨 일", "역할", "의미", "explain", "what does", "이해"]),
]
MODES = ["general", "structure", "flow", "bug", "refactor", "explain"]

STOPWORDS = {
    "the", "and", "for", "this", "that", "with", "from", "what", "how", "why", "code", "class",
    "function", "method", "keyword", "file", "analyze", "analysis", "explain", "about", "into",
    "please", "show", "find", "call", "flow", "bug", "struct", "structure", "does", "are",
    "is", "of", "in", "to", "it", "me", "a", "an", "or", "on", "be", "def", "return", "self",
}


@dataclass
class Target:
    name: str
    kind: Optional[str] = None   # "class" | "function" | None
    strong: bool = True          # 사용자가 명시적으로 지정했는지 (False면 추정값)

    @property
    def parts(self) -> List[str]:
        return [p for p in re.split(r"::|\.|->", self.name.rstrip("()")) if p]


@dataclass
class FocusSpec:
    question: str
    targets: List[Target] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    mode: str = "general"

    def is_empty(self) -> bool:
        return not (self.targets or self.keywords or self.files)

    def add_target(self, name: str, kind: Optional[str] = None, strong: bool = True) -> None:
        name = name.strip().rstrip("()").strip()
        if not name:
            return
        for t in self.targets:
            if t.name == name:
                t.kind = t.kind or kind
                t.strong = t.strong or strong
                return
        self.targets.append(Target(name, kind, strong))

    def add_keyword(self, kw: str) -> None:
        kw = kw.strip()
        if kw and kw not in self.keywords:
            self.keywords.append(kw)

    def add_file(self, f: str) -> None:
        f = f.strip()
        if f and f not in self.files:
            self.files.append(f)

    def describe(self) -> str:
        parts = [f"모드={self.mode}"]
        if self.targets:
            parts.append("대상=" + ", ".join(
                f"{t.name}" + (f"({t.kind})" if t.kind else "") + ("" if t.strong else "?")
                for t in self.targets))
        if self.keywords:
            parts.append("키워드=" + ", ".join(self.keywords))
        if self.files:
            parts.append("파일=" + ", ".join(self.files))
        return " | ".join(parts)


def detect_mode(text: str) -> str:
    low = text.lower()
    scores = {mode: sum(low.count(h) for h in hints) for mode, hints in MODE_HINTS}
    best = max(scores, key=lambda m: scores[m])
    return best if scores[best] > 0 else "general"


def _looks_like_path(s: str) -> bool:
    if "/" in s or "\\" in s:
        return True
    return bool(re.search(r"\.(py|c|cc|cpp|cxx|h|hh|hpp|hxx|java|cs|js|jsx|ts|tsx|go|kt|rs|m|mm|swift|scala)$", s))


def parse_focus(prompt: str, mode: Optional[str] = None) -> FocusSpec:
    spec = FocusSpec(question=prompt)
    rest = prompt

    # 1) 명시 태그
    for m in TAG_RE.finditer(prompt):
        tag = m.group("tag").lower()
        value = m.group("dq") or m.group("sq") or m.group("bq") or m.group("bare")
        if tag in KEYWORD_TAGS:
            spec.add_keyword(value)
        elif tag in FILE_TAGS:
            spec.add_file(value)
        elif tag in KIND_ALIASES:
            for v in value.split("|"):
                spec.add_target(v, KIND_ALIASES[tag])
        else:
            continue  # http: 같은 무관한 패턴
        rest = rest.replace(m.group(0), " ")

    # 2) 백틱
    for m in BACKTICK_RE.finditer(rest):
        v = m.group(1).strip()
        if _looks_like_path(v) and not re.search(r"::|\(\)", v):
            spec.add_file(v)
        elif re.fullmatch(rf"{IDENT}(?:(?:::|\.|->){IDENT})*(?:\(\))?", v):
            spec.add_target(v, "function" if v.endswith("()") else None)
        else:
            spec.add_keyword(v)
    rest_nobt = BACKTICK_RE.sub(" ", rest)

    # 3) 따옴표 → 키워드
    for m in QUOTE_RE.finditer(rest_nobt):
        v = next(g for g in m.groups() if g)
        spec.add_keyword(v)
    rest_noq = QUOTE_RE.sub(" ", rest_nobt)

    # 4) 한글/영문 문맥 ("Engine 클래스", "class Engine", "timeout 키워드")
    for m in CTX_RE.finditer(rest_noq):
        name = m.group("name") or m.group("name2")
        kind_word = (m.group("kind") or m.group("kind2")).lower()
        if name.lower() in STOPWORDS:
            continue
        if kind_word in ("키워드", "keyword"):
            spec.add_keyword(name)
        else:
            spec.add_target(name, KIND_ALIASES.get(kind_word))

    # 5) 식별자 추정 (약한 후보: 인덱스에 있을 때만 사용)
    for m in QUALIFIED_RE.finditer(rest_noq):
        spec.add_target(m.group(0), "function" if m.group(0).endswith("()") else None, strong=False)
    for w in WORD_RE.findall(rest_noq):
        if w.lower() not in STOPWORDS and len(w) >= 3:
            spec.add_target(w, None, strong=False)

    spec.mode = mode if mode in MODES else detect_mode(prompt)
    return spec


# ---------------------------------------------------------------- LLM 보조 추출
FOCUS_EXTRACT_PROMPT = """다음은 코드 분석 요청이다. 분석 대상이 되는 코드 요소를 JSON으로만 출력하라.
형식: {"classes": [...], "functions": [...], "keywords": [...], "files": [...]}
- classes/functions: 코드에 존재할 법한 식별자 이름(예: Engine, Engine::step, parse_config)
- keywords: 소스에서 문자열 검색할 단어/구 (변수명, 매크로, 에러 메시지, 설정 키 등)
- 확실하지 않으면 비워 둘 것. 설명 없이 JSON만 출력.

요청: """


def merge_llm_focus(spec: FocusSpec, llm_output: str) -> FocusSpec:
    """LLM이 반환한 JSON 대상 목록을 spec에 병합 (추정값이므로 strong=False)."""
    m = re.search(r"\{.*\}", llm_output, re.S)
    if not m:
        return spec
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return spec
    for name in data.get("classes") or []:
        if isinstance(name, str):
            spec.add_target(name, "class", strong=False)
    for name in data.get("functions") or []:
        if isinstance(name, str):
            spec.add_target(name, "function", strong=False)
    for kw in data.get("keywords") or []:
        if isinstance(kw, str):
            spec.add_keyword(kw)
    for f in data.get("files") or []:
        if isinstance(f, str):
            spec.add_file(f)
    return spec
