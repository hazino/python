"""CLI 진입점.

예)
  python -m code_agent ./my_repo "class:Engine 의 초기화 흐름을 분석해줘"
  python -m code_agent ./my_repo "`Parser::parse` 함수에서 버그 찾아줘" --depth 2
  python -m code_agent ./my_repo --class Engine --func step --keyword timeout "구조 설명"
  python -m code_agent ./my_repo -i          # 대화형 모드
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agent import CodeAnalysisAgent
from .config import AgentConfig
from .focus import MODES, FocusSpec
from .llm_client import LLMError

HELP_INTERACTIVE = """명령:
  /mode <general|structure|flow|bug|refactor|explain|auto>  분석 모드 고정
  /depth <n>      호출 관계 확장 깊이
  /dry            다음 질문은 LLM 호출 없이 컨텍스트만 확인 (토글)
  /outline [파일]  구조 요약 출력
  /reindex        저장소 다시 인덱싱
  /quit           종료
질문 예: class:Engine 초기화 흐름 / `Foo::bar` 버그 / keyword:"timeout" 어디서 쓰여?"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="code_agent", description="사내 로컬 LLM 기반 코드 분석 에이전트")
    ap.add_argument("repo", help="분석할 저장소(디렉터리) 또는 단일 파일 경로")
    ap.add_argument("prompt", nargs="*", help="분석 요청 (class:/func:/keyword:/file: 태그 사용 가능)")
    ap.add_argument("-c", "--config", help="설정 JSON 경로 (기본: ./code_agent.json, ~/.code_agent.json)")
    ap.add_argument("-m", "--mode", choices=MODES, help="분석 모드 (기본: 프롬프트로 자동 판단)")
    ap.add_argument("--class", dest="classes", action="append", default=[], help="집중 분석할 클래스")
    ap.add_argument("--func", dest="funcs", action="append", default=[], help="집중 분석할 함수/메서드")
    ap.add_argument("--keyword", dest="keywords", action="append", default=[], help="집중 검색 키워드")
    ap.add_argument("--file", dest="files", action="append", default=[], help="집중 분석할 파일")
    ap.add_argument("--depth", type=int, help="호출자/피호출자 확장 깊이 (기본: 설정값)")
    ap.add_argument("--llm-focus", action="store_true", help="LLM으로 분석 대상을 한 번 더 추출")
    ap.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 수집된 컨텍스트만 출력")
    ap.add_argument("--show-prompt", action="store_true", help="모델에 보내는 프롬프트 전문 출력")
    ap.add_argument("-o", "--output", help="결과를 마크다운 파일로 저장")
    ap.add_argument("-i", "--interactive", action="store_true", help="대화형 모드")
    ap.add_argument("--model", help="모델 이름 덮어쓰기")
    ap.add_argument("--base-url", help="LLM 서버 URL 덮어쓰기")
    ap.add_argument("-q", "--quiet", action="store_true", help="진행 로그 숨김")
    args = ap.parse_args(argv)

    if not Path(args.repo).exists():
        ap.error(f"경로가 존재하지 않음: {args.repo}")

    cfg = AgentConfig.load(args.config, model=args.model, base_url=args.base_url)
    log = (lambda s: None) if args.quiet else (lambda s: print(s, file=sys.stderr))
    agent = CodeAnalysisAgent(cfg, log=log)

    extra = FocusSpec(question="")
    for c in args.classes:
        extra.add_target(c, "class")
    for f in args.funcs:
        extra.add_target(f, "function")
    for k in args.keywords:
        extra.add_keyword(k)
    for f in args.files:
        extra.add_file(f)

    prompt = " ".join(args.prompt).strip()
    if args.interactive or not (prompt or not extra.is_empty()):
        return interactive(agent, args, extra)

    return run_once(agent, args, prompt or "지정한 대상을 분석해줘", extra, args.mode,
                    args.depth, args.dry_run)


def run_once(agent, args, prompt, extra, mode, depth, dry_run) -> int:
    try:
        res = agent.analyze(args.repo, prompt, mode=mode, extra_focus=extra, depth=depth,
                            use_llm_focus=args.llm_focus, dry_run=dry_run)
    except LLMError as e:
        print(f"[LLM 오류] {e}", file=sys.stderr)
        return 2
    if args.show_prompt or dry_run:
        for i, msgs in enumerate(res.messages, 1):
            print(f"\n======== 요청 {i}/{len(res.messages)} ========")
            for m in msgs:
                print(f"--- {m['role']} ---\n{m['content']}")
    if res.answer:
        print(res.answer)
        if args.output:
            Path(args.output).write_text(
                f"# 코드 분석 결과\n\n> 요청: {prompt}\n> {res.focus.describe()}\n\n{res.answer}\n",
                encoding="utf-8")
            print(f"\n저장됨: {args.output}", file=sys.stderr)
    return 0


def interactive(agent, args, extra) -> int:
    agent.index(args.repo)
    mode, depth, dry = args.mode, args.depth, args.dry_run
    print(HELP_INTERACTIVE)
    while True:
        try:
            line = input("\n분석> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.startswith("/"):
            cmd, _, val = line[1:].partition(" ")
            val = val.strip()
            if cmd in ("quit", "exit", "q"):
                return 0
            if cmd == "mode":
                mode = None if val in ("", "auto") else val if val in MODES else mode
                print(f"모드: {mode or 'auto'}")
            elif cmd == "depth" and val.isdigit():
                depth = int(val)
                print(f"확장 깊이: {depth}")
            elif cmd == "dry":
                dry = not dry
                print(f"dry-run: {dry}")
            elif cmd == "outline":
                print(agent.index(args.repo).outline(val or None))
            elif cmd == "reindex":
                agent.index(args.repo, refresh=True)
            else:
                print(HELP_INTERACTIVE)
            continue
        run_once(agent, args, line, extra, mode, depth, dry)


if __name__ == "__main__":
    sys.exit(main())
