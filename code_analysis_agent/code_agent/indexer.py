"""저장소 인덱서: 파일을 훑어 클래스/함수 심볼 테이블과 호출 관계를 만든다.

- Python: 표준 ast 모듈로 정확하게 파싱
- C 계열(C/C++/Java/C#/JS/TS/Go/Kotlin/Rust ...): 주석·문자열을 마스킹한 뒤
  정규식 + 중괄호 매칭으로 범위를 잡는 경량 파서 (외부 의존성 없음)
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from .config import AgentConfig

CLASS_KINDS = {"class", "struct", "interface", "enum", "union", "trait", "impl"}
FUNC_KINDS = {"function", "method"}

LANG_BY_EXT = {
    ".py": "python", ".c": "c", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".java": "java", ".cs": "csharp",
    ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".kt": "kotlin", ".rs": "rust", ".m": "objc", ".mm": "objc",
    ".swift": "swift", ".scala": "scala",
}

# 함수처럼 보이지만 함수 정의가 아닌 것들
NON_FUNC_NAMES = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "else", "do",
    "new", "delete", "throw", "case", "using", "typeof", "alignof", "decltype",
    "static_assert", "foreach", "lock", "synchronized", "with", "elif", "assert",
    "defined", "function", "func", "fn",
}


@dataclass(eq=False)
class Symbol:
    name: str
    kind: str              # class, struct, interface, enum, namespace, function, method
    file: str              # 저장소 기준 상대 경로
    start: int             # 1-based 시작 줄
    end: int               # 1-based 끝 줄(포함)
    parent: Optional[str] = None
    signature: str = ""
    calls: Set[str] = field(default_factory=set)
    bases: List[str] = field(default_factory=list)

    @property
    def qualname(self) -> str:
        return f"{self.parent}::{self.name}" if self.parent else self.name

    @property
    def is_class(self) -> bool:
        return self.kind in CLASS_KINDS

    @property
    def is_function(self) -> bool:
        return self.kind in FUNC_KINDS

    @property
    def span(self) -> int:
        return self.end - self.start + 1

    def key(self) -> tuple:
        return (self.file, self.start, self.end, self.name)


@dataclass
class SourceFile:
    path: str
    language: str
    lines: List[str]

    def snippet(self, start: int, end: int) -> str:
        return "\n".join(self.lines[start - 1:end])


class RepoIndex:
    def __init__(self, root: str, config: AgentConfig):
        self.root = Path(root).resolve()
        self.config = config
        self.files: Dict[str, SourceFile] = {}
        self.symbols: List[Symbol] = []
        self.by_name: Dict[str, List[Symbol]] = {}
        self.errors: List[str] = []

    # ------------------------------------------------------------------ build
    def build(self) -> "RepoIndex":
        if self.root.is_file():
            self._index_file(self.root, self.root.name)
        else:
            for path in self._iter_files():
                self._index_file(path, path.relative_to(self.root).as_posix())
        for sym in self.symbols:
            self.by_name.setdefault(sym.name, []).append(sym)
        return self

    def _iter_files(self) -> Iterable[Path]:
        exts = {e.lower() for e in self.config.include_ext}
        excluded = set(self.config.exclude_dirs)
        limit = self.config.max_file_kb * 1024
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in excluded and not d.startswith("."))
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                if p.suffix.lower() in exts:
                    try:
                        if p.stat().st_size <= limit:
                            yield p
                    except OSError:
                        continue

    def _index_file(self, path: Path, rel: str) -> None:
        try:
            raw = path.read_bytes()
        except OSError as e:
            self.errors.append(f"{rel}: {e}")
            return
        if b"\0" in raw[:4096]:
            return
        text = _decode(raw)
        lang = LANG_BY_EXT.get(path.suffix.lower(), "text")
        self.files[rel] = SourceFile(rel, lang, text.splitlines())
        try:
            if lang == "python":
                self.symbols.extend(parse_python(text, rel))
            else:
                self.symbols.extend(parse_c_like(text, rel))
        except Exception as e:  # 파싱 실패해도 키워드 검색은 가능하게 둔다
            self.errors.append(f"{rel}: 파싱 실패 ({e})")

    # ----------------------------------------------------------------- lookup
    def enclosing_symbol(self, file: str, line: int) -> Optional[Symbol]:
        """해당 줄을 포함하는 가장 안쪽 심볼(함수 우선)."""
        best = None
        for s in self.symbols:
            if s.file == file and s.start <= line <= s.end and s.kind != "namespace":
                if best is None or s.span < best.span:
                    best = s
        return best

    def members_of(self, cls: Symbol) -> List[Symbol]:
        return [s for s in self.symbols if s.parent == cls.name and s is not cls]

    def callers_of(self, sym: Symbol) -> List[Symbol]:
        return [s for s in self.symbols if s.is_function and sym.name in s.calls and s is not sym]

    def callees_of(self, sym: Symbol) -> List[Symbol]:
        out = []
        for name in sorted(sym.calls):
            cands = [c for c in self.by_name.get(name, []) if c.is_function or c.is_class]
            if not cands:
                continue
            # 같은 클래스 > 같은 파일 > 나머지 순으로 선호
            cands.sort(key=lambda c: (c.parent != sym.parent, c.file != sym.file))
            out.extend(cands[:2])
        return [c for c in out if c is not sym]

    def outline(self, file: Optional[str] = None, limit: int = 400) -> str:
        """파일/저장소 구조 요약 (심볼 목록)."""
        lines: List[str] = []
        files = [file] if file else sorted(self.files)
        for f in files:
            syms = sorted((s for s in self.symbols if s.file == f), key=lambda s: s.start)
            if not syms and file is None:
                continue
            lines.append(f"{f}")
            for s in syms:
                indent = "    " if s.parent else "  "
                sig = s.signature or s.name
                lines.append(f"{indent}[{s.kind}] {sig}  (L{s.start}-{s.end})")
                if len(lines) >= limit:
                    lines.append("  ... (생략)")
                    return "\n".join(lines)
        return "\n".join(lines)

    def stats(self) -> str:
        n_cls = sum(1 for s in self.symbols if s.is_class)
        n_fn = sum(1 for s in self.symbols if s.is_function)
        return f"파일 {len(self.files)}개, 클래스/구조체 {n_cls}개, 함수/메서드 {n_fn}개"


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp949", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ====================================================================== Python
class _PyVisitor(ast.NodeVisitor):
    def __init__(self, rel: str, lines: List[str]):
        self.rel = rel
        self.lines = lines
        self.stack: List[str] = []
        self.symbols: List[Symbol] = []

    def _start(self, node) -> int:
        decos = getattr(node, "decorator_list", [])
        return min([node.lineno] + [d.lineno for d in decos])

    def visit_ClassDef(self, node: ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
        sig = f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
        self.symbols.append(Symbol(
            node.name, "class", self.rel, self._start(node), node.end_lineno or node.lineno,
            parent=self.stack[-1] if self.stack else None, signature=sig, bases=bases,
        ))
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_func(self, node):
        parent = self.stack[-1] if self.stack else None
        header = self.lines[node.lineno - 1].strip() if node.lineno <= len(self.lines) else node.name
        calls = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name):
                    calls.add(f.id)
                elif isinstance(f, ast.Attribute):
                    calls.add(f.attr)
        self.symbols.append(Symbol(
            node.name, "method" if parent else "function", self.rel,
            self._start(node), node.end_lineno or node.lineno,
            parent=parent, signature=header.rstrip(":"), calls=calls,
        ))
        # 중첩 함수는 부모 함수 이름을 parent로
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func


def parse_python(text: str, rel: str) -> List[Symbol]:
    tree = ast.parse(text)
    v = _PyVisitor(rel, text.splitlines())
    v.visit(tree)
    return v.symbols


# ===================================================================== C-like
def mask_source(text: str) -> str:
    """주석과 문자열 리터럴을 공백으로 치환 (길이와 줄바꿈은 보존)."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c in "\"'`":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n" and c != "`":
                    break  # 닫히지 않은 리터럴(예: Rust lifetime 'a) 은 줄 끝에서 중단
                j += 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _match_brace(masked: str, open_pos: int) -> int:
    depth = 0
    for i in range(open_pos, len(masked)):
        ch = masked[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(masked) - 1


CLASS_RE = re.compile(
    r"\b(?P<kind>class|struct|interface|enum(?:\s+class)?|union|namespace|trait|impl)\s+"
    r"(?P<name>[A-Za-z_]\w*)(?P<rest>[^;{()=]*)\{"
)
FUNC_RE = re.compile(
    r"(?P<name>~?[A-Za-z_]\w*(?:\s*(?:::|\.)\s*~?[A-Za-z_]\w*)*)\s*"
    r"(?:<[^<>;{}()]*>\s*)?"                      # 제네릭/템플릿 인자
    r"\((?P<params>[^()]*(?:\([^()]*\)[^()]*)*)\)"
    r"(?P<tail>(?:[^;{}()]|\([^()]*\))*?)\{"      # const/override/throws/-> T/초기화 리스트
)
CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def parse_c_like(text: str, rel: str) -> List[Symbol]:
    masked = mask_source(text)
    line_starts = [0]
    for m in re.finditer("\n", text):
        line_starts.append(m.end())

    def line_of(pos: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def line_text(pos: int) -> str:
        ln = line_of(pos)
        end = line_starts[ln] if ln < len(line_starts) else len(text)
        return text[line_starts[ln - 1]:end]

    symbols: List[Symbol] = []
    containers: List[Symbol] = []

    for m in CLASS_RE.finditer(masked):
        open_pos = m.end() - 1
        close = _match_brace(masked, open_pos)
        kind = m.group("kind").split()[0]
        rest = " ".join(m.group("rest").split())
        bases = []
        if rest:
            bases = [b for b in re.split(r"[,:\s]+", rest)
                     if b and b not in ("public", "private", "protected", "virtual",
                                        "extends", "implements", "final", "for")]
        sym = Symbol(
            m.group("name"), kind, rel, line_of(m.start()), line_of(close),
            signature=" ".join(f"{m.group('kind')} {m.group('name')} {rest}".split()),
            bases=bases,
        )
        symbols.append(sym)
        containers.append(sym)
        sym._range = (open_pos, close)  # type: ignore[attr-defined]

    pos = 0
    while True:
        m = FUNC_RE.search(masked, pos)
        if not m:
            break
        # 거부 시 이름 바로 뒤부터 다시 탐색 (예: Go "func (r *T) Name(...)" 의 func)
        pos = m.end("name")
        full = re.sub(r"\s+", "", m.group("name"))
        parts = re.split(r"::|\.", full)
        name = parts[-1]
        if name.lstrip("~") in NON_FUNC_NAMES or full in NON_FUNC_NAMES:
            continue
        # 이름 앞이 연산자(대입식 우변 등)면 함수 정의가 아님. 단 반환형의 &, *, > 는 허용
        before = masked[max(0, m.start() - 200):m.start()].rstrip()
        if before and before[-1] in "=+-/%!|^?<,(" :
            continue
        open_pos = m.end() - 1
        close = _match_brace(masked, open_pos)
        body = masked[open_pos:close + 1]
        calls = {c for c in CALL_RE.findall(body) if c not in NON_FUNC_NAMES}

        parent = parts[-2] if len(parts) >= 2 else None
        if parent is None:
            inner = [c for c in containers
                     if c.kind != "namespace" and c._range[0] < m.start() < c._range[1]]  # type: ignore[attr-defined]
            if inner:
                parent = min(inner, key=lambda c: c._range[1] - c._range[0]).name  # type: ignore[attr-defined]

        start_line = line_of(m.start())
        header = line_text(m.start()).strip()
        if "{" not in header and ")" not in header:
            header = " ".join(text[line_starts[start_line - 1]:open_pos].split())
        sig = header.split("{")[0].strip()
        symbols.append(Symbol(
            name, "method" if parent else "function", rel, start_line, line_of(close),
            parent=parent, signature=sig[:200], calls=calls,
        ))
        pos = close + 1  # 함수 본문 내부는 건너뜀

    for s in symbols:
        if hasattr(s, "_range"):
            del s._range  # type: ignore[attr-defined]
    return symbols
