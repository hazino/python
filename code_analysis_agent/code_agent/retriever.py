"""FocusSpec → 실제 코드 조각(ContextItem) 수집 및 컨텍스트 예산 패킹.

우선순위(priority, 작을수록 중요)
  0  사용자가 지정한 대상의 본문 (클래스/함수 전체 소스)
  1  지정 파일의 구조 요약 / 키워드가 등장하는 심볼
  2  지정 클래스의 외부 정의 메서드 (C++ Foo::bar 등)
  3  호출자 / 피호출자 (1-hop, expand_depth 만큼)
  4  관련 파일 구조 요약 (outline)
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .focus import FocusSpec, Target
from .indexer import RepoIndex, Symbol

LABELS = {0: "주요 대상", 1: "관련(키워드/파일)", 2: "멤버", 3: "호출 관계", 4: "구조 요약"}


@dataclass
class ContextItem:
    priority: int
    title: str
    file: str
    start: int
    end: int
    text: str
    reason: str = ""
    language: str = ""

    def render(self) -> str:
        width = len(str(self.end))
        numbered = "\n".join(
            f"{str(self.start + i).rjust(width)}| {line}"
            for i, line in enumerate(self.text.splitlines())
        ) if self.start > 0 else self.text
        loc = f"{self.file}:{self.start}-{self.end}" if self.start > 0 else self.file
        head = f"### [{LABELS.get(self.priority, '참고')}] {self.title} ({loc})"
        if self.reason:
            head += f"\n- 포함 이유: {self.reason}"
        return f"{head}\n```{self.language}\n{numbered}\n```"

    def size(self) -> int:
        return len(self.text) + len(self.title) + 80


@dataclass
class Retrieval:
    items: List[ContextItem] = field(default_factory=list)
    matched: List[Symbol] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)     # 사용자/모델에게 알릴 매칭 정보
    unresolved: List[str] = field(default_factory=list)


class Retriever:
    def __init__(self, index: RepoIndex):
        self.index = index
        self.cfg = index.config

    # ------------------------------------------------------------ resolution
    def resolve_target(self, t: Target) -> Tuple[List[Symbol], str]:
        """대상 이름 → 심볼 목록. (결과, 매칭 방식) 반환."""
        parts = t.parts
        if not parts:
            return [], ""
        name, parent = parts[-1], (parts[-2] if len(parts) >= 2 else None)

        def kind_ok(s: Symbol) -> bool:
            if t.kind == "class":
                return s.is_class
            if t.kind == "function":
                return s.is_function
            return s.kind != "namespace"

        def parent_ok(s: Symbol) -> bool:
            return parent is None or s.parent == parent or s.file.rsplit("/", 1)[-1].split(".")[0] == parent

        exact = [s for s in self.index.by_name.get(name, []) if kind_ok(s) and parent_ok(s)]
        if exact:
            return exact, "exact"
        if not t.strong:
            return [], ""
        low = name.lower()
        ci = [s for s in self.index.symbols if s.name.lower() == low and kind_ok(s) and parent_ok(s)]
        if ci:
            return ci, "대소문자 무시"
        names = sorted({s.name for s in self.index.symbols if kind_ok(s)})
        close = difflib.get_close_matches(name, names, n=3, cutoff=0.75)
        partial = [n for n in names if low in n.lower()][:5] if len(low) >= 4 else []
        picked = list(dict.fromkeys(close + partial))
        found = [s for n in picked for s in self.index.by_name.get(n, []) if kind_ok(s)]
        return found[:8], "유사 이름" if found else ""

    # -------------------------------------------------------------- retrieve
    def retrieve(self, spec: FocusSpec, depth: Optional[int] = None) -> Retrieval:
        depth = self.cfg.expand_depth if depth is None else depth
        r = Retrieval()

        def add_symbol(sym: Symbol, prio: int, reason: str) -> None:
            # 같거나 더 중요한 항목이 이미 이 범위를 포함하면 생략
            for it in r.items:
                if it.file == sym.file and it.start > 0 and it.start <= sym.start \
                        and sym.end <= it.end and it.priority <= prio:
                    return
            # 새 항목이 포함하는 덜 중요한(또는 같은) 기존 항목은 제거
            r.items = [it for it in r.items if not (
                it.file == sym.file and it.start > 0 and sym.start <= it.start
                and it.end <= sym.end and it.priority >= prio)]
            src = self.index.files[sym.file]
            r.items.append(ContextItem(
                prio, f"{sym.kind} {sym.qualname}", sym.file, sym.start, sym.end,
                src.snippet(sym.start, sym.end), reason, src.language,
            ))

        # 1) 지정 대상
        for t in spec.targets:
            syms, how = self.resolve_target(t)
            if not syms:
                if t.strong:
                    r.unresolved.append(t.name)
                continue
            if how != "exact":
                r.notes.append(f"'{t.name}' 정확히 일치하는 심볼 없음 → {how} 매칭: "
                               + ", ".join(sorted({s.qualname for s in syms})))
            for s in syms:
                r.matched.append(s)
                add_symbol(s, 0, f"요청 대상 '{t.name}'")

        # 2) 키워드
        for kw in spec.keywords:
            hits = self.keyword_hits(kw)
            if not hits:
                r.unresolved.append(f'"{kw}"')
                continue
            r.notes.append(f"키워드 '{kw}': {len(hits)}곳에서 발견")
            loose: Dict[str, List[int]] = {}
            for file, line in hits:
                enc = self.index.enclosing_symbol(file, line)
                if enc and enc.span <= 400:
                    add_symbol(enc, 1, f"키워드 '{kw}' 포함 (L{line})")
                    r.matched.append(enc)
                else:
                    loose.setdefault(file, []).append(line)
            for file, lines in loose.items():
                for s, e in _merge_ranges(lines, 4, len(self.index.files[file].lines)):
                    src = self.index.files[file]
                    r.items.append(ContextItem(1, f"'{kw}' 등장 위치", file, s, e,
                                               src.snippet(s, e), "키워드 검색", src.language))

        # 3) 지정 파일
        for f in spec.files:
            matches = [p for p in self.index.files if p == f or p.endswith("/" + f) or p.endswith(f)]
            if not matches:
                r.unresolved.append(f"file:{f}")
                continue
            for p in matches[:3]:
                src = self.index.files[p]
                if len("\n".join(src.lines)) <= self.cfg.context_chars // 2:
                    r.items.append(ContextItem(0, f"파일 {p}", p, 1, len(src.lines),
                                               "\n".join(src.lines), "요청 파일", src.language))
                else:
                    r.items.append(ContextItem(1, f"파일 구조 {p}", p, 0, 0,
                                               self.index.outline(p), "요청 파일(크기가 커서 구조만)"))
                    for s in self.index.symbols:
                        if s.file == p and s.parent is None:
                            r.matched.append(s)

        # 4) 확장: 멤버 / 호출 관계
        frontier = list(dict.fromkeys(r.matched))
        related = 0
        for level in range(depth):
            nxt: List[Symbol] = []
            for s in frontier:
                if s.is_class:
                    for m in self.index.members_of(s):
                        if m.file != s.file or not (s.start <= m.start <= s.end):
                            add_symbol(m, 2, f"{s.name}의 멤버 (외부 정의)")
                    continue
                for c in self.index.callees_of(s)[:6]:
                    if related >= self.cfg.max_related:
                        break
                    if c.span <= 200:
                        add_symbol(c, 3, f"{s.qualname}에서 호출")
                        related += 1
                        nxt.append(c)
                for c in self.index.callers_of(s)[:6]:
                    if related >= self.cfg.max_related:
                        break
                    if c.span <= 200:
                        add_symbol(c, 3, f"{s.qualname}을(를) 호출")
                        related += 1
                        nxt.append(c)
            frontier = nxt

        # 5) 구조 요약: 관련 파일 outline (대상이 없으면 저장소 전체)
        files = list(dict.fromkeys(it.file for it in r.items if it.start > 0))
        if files:
            outline = "\n".join(self.index.outline(f, limit=60) for f in files[:6])
            r.items.append(ContextItem(4, "관련 파일 구조", ", ".join(files[:6]), 0, 0, outline))
        else:
            r.notes.append("지정한 대상을 코드에서 찾지 못해 저장소 전체 구조를 기반으로 분석합니다.")
            r.items.append(ContextItem(4, "저장소 구조", str(self.index.root.name), 0, 0,
                                       self.index.outline(limit=600)))
        r.items.sort(key=lambda it: (it.priority, it.file, it.start))
        return r

    def keyword_hits(self, kw: str) -> List[Tuple[str, int]]:
        if re.fullmatch(r"\w+", kw):
            pat = re.compile(rf"\b{re.escape(kw)}\b", re.I)
        else:
            pat = re.compile(re.escape(kw), re.I)
        hits = []
        for path, src in self.index.files.items():
            for i, line in enumerate(src.lines, 1):
                if pat.search(line):
                    hits.append((path, i))
                    if len(hits) >= self.cfg.max_keyword_hits:
                        return hits
        return hits


def _merge_ranges(lines: List[int], ctx: int, maxline: int) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for ln in sorted(lines):
        s, e = max(1, ln - ctx), min(maxline, ln + ctx)
        if out and s <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


# ------------------------------------------------------------------ packing
def pack(items: List[ContextItem], budget: int, max_batches: int = 8) -> List[List[ContextItem]]:
    """우선순위 순으로 예산 내에 담는다.

    - 전부 한 번에 들어가면 배치 1개.
    - 핵심(priority<=1)만으로 예산 초과 시: 여러 배치로 나눔 (map-reduce 용).
      너무 큰 단일 항목은 줄 단위로 분할한다.
    - 보조 항목(priority>=2)은 남는 공간에만 채운다.
    """
    core = [it for it in items if it.priority <= 1]
    extra = [it for it in items if it.priority > 1]

    split_core: List[ContextItem] = []
    for it in core:
        split_core.extend(_split_item(it, budget))

    batches: List[List[ContextItem]] = [[]]
    used = [0]
    for it in split_core:
        if used[-1] + it.size() > budget and batches[-1]:
            batches.append([])
            used.append(0)
        batches[-1].append(it)
        used[-1] += it.size()

    # 보조 항목: 첫 배치부터 남는 공간에 채움. 배치가 1개면 그 배치에만.
    for it in extra:
        for i, b in enumerate(batches):
            if used[i] + it.size() <= budget:
                b.append(it)
                used[i] += it.size()
                break
    if len(batches) > max_batches:
        batches = batches[:max_batches]
    return [b for b in batches if b]


def _split_item(it: ContextItem, budget: int) -> List[ContextItem]:
    if it.size() <= budget or it.start <= 0:
        return [it]
    def chunk(start: int, body: List[str]) -> ContextItem:
        return ContextItem(it.priority, f"{it.title} (부분)", it.file, start,
                           start + len(body) - 1, "\n".join(body), it.reason, it.language)

    out: List[ContextItem] = []
    cur: List[str] = []
    cur_start, size = it.start, 0
    for i, line in enumerate(it.text.splitlines()):
        if size + len(line) + 1 > budget - 200 and cur:
            out.append(chunk(cur_start, cur))
            cur_start, cur, size = it.start + i, [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        out.append(chunk(cur_start, cur))
    return out
