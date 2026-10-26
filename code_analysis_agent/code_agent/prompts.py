"""분석 모드별 프롬프트 템플릿."""
from __future__ import annotations

from typing import List

SYSTEM = """당신은 대규모 코드베이스를 분석하는 시니어 소프트웨어 엔지니어다.
규칙:
1. 제공된 코드 컨텍스트에 근거해서만 답한다. 컨텍스트에 없는 내용은 추측하지 말고 "제공된 코드에서 확인 불가"라고 쓴다.
2. 사용자가 지정한 [주요 대상]을 중심으로 분석하고, [멤버]/[호출 관계]/[구조 요약]은 그 대상을 이해하는 데 필요한 만큼만 언급한다.
3. 근거를 제시할 때는 반드시 `파일경로:줄번호` 형식으로 위치를 인용한다.
4. 답변은 {language_name}로, 마크다운 형식으로 작성한다.
"""

MODE_INSTRUCTIONS = {
    "general": """요청에 맞게 분석하라. 다음을 포함:
- 대상 요약 (역할/책임 2~3줄)
- 핵심 동작과 주요 로직
- 다른 코드와의 관계 (누가 호출하고 무엇을 호출하는지)
- 주의할 점이나 개선 포인트(있다면)""",
    "structure": """구조 분석을 수행하라:
- 클래스/모듈 계층과 상속·구성(composition) 관계
- 각 클래스의 책임, 주요 멤버 변수/메서드와 공개 인터페이스
- 의존 방향과 결합도
- 가능하면 mermaid classDiagram 으로 관계를 도식화""",
    "flow": """실행 흐름 분석을 수행하라:
- 진입점부터 종료까지 단계별 호출 순서 (번호 목록, 각 단계에 파일:줄)
- 분기 조건, 루프, 예외/에러 처리 경로
- 상태(멤버 변수, 전역 변수)가 어떻게 바뀌는지
- 가능하면 mermaid sequenceDiagram 또는 flowchart 로 도식화""",
    "bug": """결함 분석을 수행하라:
- 잠재 버그를 심각도(높음/중간/낮음)와 함께 나열: 위치, 원인, 재현 조건, 수정 제안
- 경계값, null/None, 자원 해제, 동시성, 예외 처리, 정수 오버플로, 인덱스 범위를 중점 확인
- 확신이 낮은 항목은 '의심'으로 표시""",
    "refactor": """개선/리팩터링 관점으로 분석하라:
- 가독성, 중복, 책임 분리, 성능 병목, 테스트 용이성 문제
- 각 문제에 대해 구체적인 수정안(필요하면 짧은 코드 예시)
- 변경 시 영향 범위(호출자)를 함께 명시""",
    "explain": """대상 코드를 처음 보는 개발자에게 설명하라:
- 이 코드가 무엇을 하는지 한 문단 요약
- 줄/블록 단위로 핵심 부분 설명
- 사용 예시(호출 방법)와 입력/출력""",
}

MAP_INSTRUCTION = """이 컨텍스트는 전체 분석 대상 중 일부({i}/{n})다.
사용자 요청과 관련된 사실만 간결하게 추출하라 (각 항목에 파일:줄 인용).
최종 답변을 작성하지 말고, 나중에 다른 부분과 합칠 '분석 메모'만 작성하라."""

REDUCE_INSTRUCTION = """아래는 코드의 여러 부분을 나눠 분석한 메모들이다.
이 메모들을 종합해 사용자 요청에 대한 최종 답변을 작성하라. 메모 간 중복은 합치고, 모순이 있으면 명시하라."""

LANG_NAMES = {"ko": "한국어", "en": "English", "ja": "日本語"}


def system_prompt(language: str) -> str:
    return SYSTEM.format(language_name=LANG_NAMES.get(language, language))


def build_user_prompt(question: str, mode: str, focus_desc: str, notes: List[str],
                      context: str, map_part: tuple | None = None) -> str:
    parts = [f"## 사용자 요청\n{question}", f"## 분석 초점\n{focus_desc}"]
    if notes:
        parts.append("## 대상 매칭 정보\n" + "\n".join(f"- {n}" for n in notes))
    parts.append(f"## 코드 컨텍스트\n{context}")
    if map_part:
        parts.append("## 지시\n" + MAP_INSTRUCTION.format(i=map_part[0], n=map_part[1]))
    else:
        parts.append("## 지시\n" + MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["general"]))
    return "\n\n".join(parts)


def build_reduce_prompt(question: str, mode: str, focus_desc: str, memos: List[str]) -> str:
    memo_txt = "\n\n".join(f"### 메모 {i}\n{m}" for i, m in enumerate(memos, 1))
    return (f"## 사용자 요청\n{question}\n\n## 분석 초점\n{focus_desc}\n\n"
            f"## 부분 분석 메모\n{memo_txt}\n\n## 지시\n{REDUCE_INSTRUCTION}\n\n"
            + MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["general"]))
