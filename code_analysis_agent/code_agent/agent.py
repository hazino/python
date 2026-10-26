"""코드 분석 에이전트: 인덱싱 → 초점 추출 → 검색/확장 → 컨텍스트 패킹 → LLM 분석."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import prompts
from .config import AgentConfig
from .focus import FOCUS_EXTRACT_PROMPT, FocusSpec, merge_llm_focus, parse_focus
from .indexer import RepoIndex
from .llm_client import LLMClient
from .retriever import ContextItem, Retrieval, Retriever, pack


@dataclass
class AnalysisResult:
    answer: str
    focus: FocusSpec
    retrieval: Retrieval
    batches: List[List[ContextItem]] = field(default_factory=list)
    messages: List[List[Dict[str, str]]] = field(default_factory=list)

    def context_summary(self) -> str:
        lines = [f"초점: {self.focus.describe()}"]
        for n in self.retrieval.notes:
            lines.append(f"  - {n}")
        if self.retrieval.unresolved:
            lines.append("  - 찾지 못한 대상: " + ", ".join(self.retrieval.unresolved))
        for bi, b in enumerate(self.batches, 1):
            size = sum(it.size() for it in b)
            lines.append(f"  배치 {bi}/{len(self.batches)} (~{size:,}자)")
            for it in b:
                loc = f"{it.file}:{it.start}-{it.end}" if it.start > 0 else it.file
                lines.append(f"    [{it.priority}] {it.title}  {loc}")
        return "\n".join(lines)


class CodeAnalysisAgent:
    def __init__(self, config: AgentConfig, llm: Optional[LLMClient] = None,
                 log: Callable[[str], None] = lambda s: None):
        self.config = config
        self.llm = llm or LLMClient(config)
        self.log = log
        self._indexes: Dict[str, RepoIndex] = {}

    def index(self, repo: str, refresh: bool = False) -> RepoIndex:
        if refresh or repo not in self._indexes:
            self.log(f"인덱싱 중: {repo}")
            idx = RepoIndex(repo, self.config).build()
            self.log(f"인덱싱 완료: {idx.stats()}")
            for e in idx.errors[:5]:
                self.log(f"  경고: {e}")
            self._indexes[repo] = idx
        return self._indexes[repo]

    def build_focus(self, prompt: str, mode: Optional[str] = None, use_llm: bool = False,
                    extra: Optional[FocusSpec] = None) -> FocusSpec:
        spec = parse_focus(prompt, mode)
        if extra:
            for t in extra.targets:
                spec.add_target(t.name, t.kind, t.strong)
            for k in extra.keywords:
                spec.add_keyword(k)
            for f in extra.files:
                spec.add_file(f)
        if use_llm:
            out = self.llm.chat([{"role": "user", "content": FOCUS_EXTRACT_PROMPT + prompt}],
                                max_tokens=400)
            merge_llm_focus(spec, out)
        return spec

    def analyze(self, repo: str, prompt: str, mode: Optional[str] = None,
                extra_focus: Optional[FocusSpec] = None, depth: Optional[int] = None,
                use_llm_focus: bool = False, dry_run: bool = False) -> AnalysisResult:
        idx = self.index(repo)
        spec = self.build_focus(prompt, mode, use_llm_focus, extra_focus)
        retrieval = Retriever(idx).retrieve(spec, depth)
        # 약한 후보 중 실제로 매칭되지 않은 것은 초점 표시에서 제외
        matched_names = {s.name for s in retrieval.matched}
        spec.targets = [t for t in spec.targets if t.strong or (t.parts and t.parts[-1] in matched_names)]

        batches = pack(retrieval.items, self.config.context_chars, self.config.max_map_batches)
        result = AnalysisResult("", spec, retrieval, batches)
        self.log(result.context_summary())

        sys_msg = {"role": "system", "content": prompts.system_prompt(self.config.language)}
        focus_desc = spec.describe()
        notes = retrieval.notes + (
            ["찾지 못한 대상: " + ", ".join(retrieval.unresolved)] if retrieval.unresolved else [])

        def ctx(batch: List[ContextItem]) -> str:
            return "\n\n".join(it.render() for it in batch)

        if len(batches) <= 1:
            msgs = [sys_msg, {"role": "user", "content": prompts.build_user_prompt(
                prompt, spec.mode, focus_desc, notes, ctx(batches[0] if batches else []))}]
            result.messages.append(msgs)
            if not dry_run:
                result.answer = self.llm.chat(msgs)
            return result

        # map-reduce: 컨텍스트가 예산을 넘으면 부분 분석 후 종합
        memos = []
        for i, b in enumerate(batches, 1):
            msgs = [sys_msg, {"role": "user", "content": prompts.build_user_prompt(
                prompt, spec.mode, focus_desc, notes, ctx(b), map_part=(i, len(batches)))}]
            result.messages.append(msgs)
            if not dry_run:
                self.log(f"부분 분석 {i}/{len(batches)} ...")
                memos.append(self.llm.chat(msgs))
        reduce_msgs = [sys_msg, {"role": "user", "content": prompts.build_reduce_prompt(
            prompt, spec.mode, focus_desc, memos or ["(dry-run)"])}]
        result.messages.append(reduce_msgs)
        if not dry_run:
            self.log("종합 중 ...")
            result.answer = self.llm.chat(reduce_msgs)
        return result
