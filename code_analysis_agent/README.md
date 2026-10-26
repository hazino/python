# Code Analysis Agent (사내 로컬 LLM)

사내 로컬 모델(OpenAI 호환 API 또는 Ollama)에 붙여 코드를 분석하는 에이전트.
프롬프트에서 **클래스 / 함수 / 키워드 / 파일**을 지정하면, 그 부분의 소스를 정확히 찾아서
관련 코드(멤버, 호출자, 피호출자)와 함께 모델에 전달하므로 **지정 대상을 집중적으로 분석**합니다.

- 외부 패키지 없음 (Python 3.9+ 표준 라이브러리만 사용). 폐쇄망에서 그대로 사용 가능
- Python은 `ast`로 정확하게, C/C++/Java/C#/JS/TS/Go/Kotlin/Rust 등은 경량 파서로 인덱싱
- 컨텍스트가 모델 한도를 넘으면 자동으로 **map-reduce**(부분 분석 → 종합) 처리

## 동작 구조

```
프롬프트 ──► focus.py      대상 추출 (class:/func:/keyword:/file:, 백틱, "따옴표", "Foo 클래스" ...)
                │           분석 모드 추정 (structure / flow / bug / refactor / explain / general)
저장소  ──► indexer.py    심볼 테이블 + 호출 관계 (파일, 줄 범위, 부모 클래스, 시그니처, 호출 목록)
                │
            retriever.py   대상 → 소스 본문 [우선순위 0]
                │           키워드 → 등장 위치를 포함하는 함수/클래스 [1]
                │           클래스 → 외부 정의 메서드(C++ Foo::bar) [2]
                │           함수 → 호출자/피호출자 (--depth) [3]
                │           관련 파일 구조 요약 [4]
                │           → 우선순위 순으로 context_chars 예산 안에 패킹
            agent.py       1배치: 바로 분석 / 다배치: 부분 분석 메모 → 종합
                │
            llm_client.py  사내 LLM 호출 (openai 호환 /chat/completions 또는 ollama /api/chat)
```

## 설정

`config.example.json`을 `code_agent.json`(실행 위치) 또는 `~/.code_agent.json`으로 복사해서 수정합니다.

| 키 | 설명 |
|---|---|
| `provider` | `openai`(vLLM, TGI, LM Studio, LiteLLM, sglang 등 OpenAI 호환) 또는 `ollama` |
| `base_url` | 예: `http://llm.internal:8000/v1` (openai), `http://llm.internal:11434` (ollama) |
| `model` | 서버에 올라간 모델 이름 |
| `api_key` | 필요 시. `Authorization: Bearer` 헤더로 전송 |
| `extra_headers` | 사내 게이트웨이용 추가 헤더 (예: `{"x-api-key": "..."}`) |
| `verify_ssl` / `ca_bundle` | 사내 인증서 환경용 |
| `context_chars` | 한 요청에 넣을 코드 컨텍스트 문자 수. **모델 컨텍스트 길이에 맞춰 조정** (8K 토큰 모델 ≈ 16000, 32K ≈ 60000) |
| `expand_depth` | 호출 관계 확장 깊이 (기본 1) |
| `language` | 답변 언어 (`ko`, `en`) |

환경변수로도 덮어쓸 수 있습니다: `CODE_AGENT_BASE_URL`, `CODE_AGENT_MODEL`, `CODE_AGENT_API_KEY`,
`CODE_AGENT_PROVIDER`, `CODE_AGENT_CONTEXT_CHARS`, `CODE_AGENT_TIMEOUT`.

## 사용법

```bash
cd code_analysis_agent

# 태그로 대상 지정
python -m code_agent /path/to/repo "class:Engine 의 초기화 흐름을 분석해줘"
python -m code_agent /path/to/repo "func:Parser::parse 에서 버그 찾아줘"
python -m code_agent /path/to/repo 'keyword:"timeout" 이 어디서 어떻게 쓰이는지'
python -m code_agent /path/to/repo "file:src/net/socket.cpp 구조 설명"

# 자연어 + 백틱/따옴표
python -m code_agent /path/to/repo "Session 클래스와 \`retry()\` 의 관계를 구조 관점에서"

# 옵션으로 지정 (여러 개 가능)
python -m code_agent /path/to/repo --class Engine --func step --keyword dt "리팩터링 포인트"

# 모드 / 확장 깊이 고정, 결과 저장
python -m code_agent /path/to/repo "func:main" -m flow --depth 2 -o report.md

# 모델 호출 없이 어떤 코드가 들어가는지 확인 (프롬프트 튜닝용)
python -m code_agent /path/to/repo "class:Engine" --dry-run

# 대화형 (인덱스 재사용)
python -m code_agent /path/to/repo -i
```

### 대상 지정 문법

| 방식 | 예 | 해석 |
|---|---|---|
| 태그 | `class:Engine`, `클래스:Engine` | 클래스/구조체/인터페이스 |
| | `func:step`, `method:Engine::step`, `함수:run` | 함수/메서드 (`A::b`, `A.b` 둘 다 가능) |
| | `keyword:"retry count"`, `키워드:TIMEOUT` | 소스 전체 문자열 검색 → 등장한 함수 포함 |
| | `file:engine.cpp`, `파일:src/a.py` | 파일 (작으면 전문, 크면 구조 요약) |
| | `symbol:Foo` | 종류 무관 |
| | `class:A\|B` | 여러 개 |
| 백틱 | `` `Engine::step` ``, `` `parse()` `` | 심볼 |
| 따옴표 | `"connection reset"` | 키워드 |
| 한글 문맥 | `Engine 클래스`, `step 함수`, `timeout 키워드` | 해당 종류로 지정 |
| 식별자 추정 | `helper 는 뭐 해?` | 저장소에 **실제로 있는** 이름일 때만 대상이 됨 |

정확히 일치하는 심볼이 없으면 대소문자 무시 → 유사 이름 순으로 찾고, 어떤 것으로 매칭했는지
모델과 사용자 모두에게 알려줍니다. 끝내 못 찾으면 저장소 구조 요약을 기반으로 분석합니다.

### 분석 모드

프롬프트 단어로 자동 판단하며 `-m`으로 고정할 수 있습니다.

| 모드 | 트리거 단어 예 | 출력 |
|---|---|---|
| `structure` | 구조, 아키텍처, 상속, 의존 | 계층/책임/의존 + mermaid classDiagram |
| `flow` | 흐름, 호출, 시퀀스, 처리 과정 | 단계별 호출 순서 + mermaid sequence/flowchart |
| `bug` | 버그, 오류, 크래시, 누수 | 심각도별 결함 목록과 수정안 |
| `refactor` | 리팩, 개선, 최적화, 성능 | 문제점·수정안·영향 범위 |
| `explain` | 설명, 역할, 의미 | 입문자용 설명 |
| `general` | (기본) | 요약/핵심 로직/관계/주의점 |

## 파이썬에서 사용

```python
from code_agent import AgentConfig, CodeAnalysisAgent

agent = CodeAnalysisAgent(AgentConfig.load("code_agent.json"))
res = agent.analyze("/path/to/repo", "class:Engine 구조 분석", depth=2)
print(res.answer)
print(res.context_summary())   # 어떤 코드가 컨텍스트에 들어갔는지
```

`LLMClient` 대신 `chat(messages, max_tokens=None) -> str` 메서드를 가진 객체를
`CodeAnalysisAgent(config, llm=...)`로 넘기면 다른 사내 SDK나 클라이언트에도 연결할 수 있습니다.

## 테스트

```bash
python -m unittest discover -s tests -v
```

## 한계

- C 계열 파서는 정규식과 중괄호 매칭을 쓰는 경량 파서입니다. 매크로가 많은 코드나 `#ifdef`로 중괄호
  짝이 갈리는 코드에서는 범위가 틀릴 수 있습니다. 이런 경우에도 `keyword:` 검색은 동작합니다.
- 호출 관계는 이름 기준이라 오버로드나 동명 메서드를 구분하지 못할 수 있습니다
  (같은 클래스 → 같은 파일 순으로 우선 선택).
