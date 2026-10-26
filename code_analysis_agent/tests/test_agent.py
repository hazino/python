import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_agent import AgentConfig, CodeAnalysisAgent, parse_focus  # noqa: E402
from code_agent.indexer import RepoIndex, parse_c_like  # noqa: E402
from code_agent.llm_client import LLMClient  # noqa: E402
from code_agent.retriever import Retriever, pack  # noqa: E402

CPP_HEADER = """\
#pragma once
// class Fake { };  주석 안의 클래스는 무시
class Engine : public Base {
public:
    Engine(int n);
    void step(double dt);
    int count() const { return count_; }
private:
    int count_;
};
"""

CPP_SOURCE = """\
#include "engine.h"
Engine::Engine(int n) : count_(n), name_("x{") {
    init();
}

void Engine::step(double dt) {
    if (dt > 0) {
        update(dt);
    }
    const char* s = "void fake() {";
}

static void update(double dt) {
    log_timeout(dt);
}

void runLoop() {
    Engine e(3);
    while (true) { e.step(0.1); }
}
"""

PY_SOURCE = '''\
import time

TIMEOUT = 30


class Session:
    """세션."""

    def __init__(self, host):
        self.host = host

    @property
    def ready(self):
        return self.connect()

    def connect(self):
        return retry(self._open, TIMEOUT)

    def _open(self):
        time.sleep(0)
        return True


def retry(fn, timeout):
    return fn()
'''

GO_SOURCE = """\
package main

func (s *Server) Handle(req Request) error {
    return s.process(req)
}

func helper(x int) int {
    if x > 0 { return x }
    return 0
}
"""


def make_repo(tmp: Path) -> Path:
    (tmp / "src").mkdir()
    (tmp / "src" / "engine.h").write_text(CPP_HEADER)
    (tmp / "src" / "engine.cpp").write_text(CPP_SOURCE)
    (tmp / "py").mkdir()
    (tmp / "py" / "session.py").write_text(PY_SOURCE)
    (tmp / "server.go").write_text(GO_SOURCE)
    (tmp / "build").mkdir()
    (tmp / "build" / "gen.py").write_text("def generated():\n    pass\n")
    return tmp


class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, messages, max_tokens=None):
        self.calls.append(messages)
        return f"answer-{len(self.calls)}"


class FocusTests(unittest.TestCase):
    def test_tags(self):
        s = parse_focus('class:Engine func:Engine::step keyword:"time out" file:src/engine.cpp 흐름 분석')
        names = {(t.name, t.kind, t.strong) for t in s.targets}
        self.assertIn(("Engine", "class", True), names)
        self.assertIn(("Engine::step", "function", True), names)
        self.assertEqual(s.keywords, ["time out"])
        self.assertEqual(s.files, ["src/engine.cpp"])
        self.assertEqual(s.mode, "flow")

    def test_korean_context_and_backticks(self):
        s = parse_focus("Session 클래스와 `retry()` 의 구조를 설명해줘, timeout 키워드도")
        strong = {t.name: t.kind for t in s.targets if t.strong}
        self.assertEqual(strong.get("Session"), "class")
        self.assertEqual(strong.get("retry"), "function")
        self.assertIn("timeout", s.keywords)
        self.assertEqual(s.mode, "structure")

    def test_korean_tags_and_bug_mode(self):
        s = parse_focus("함수:connect 에서 버그 찾아줘")
        self.assertEqual([(t.name, t.kind) for t in s.targets if t.strong], [("connect", "function")])
        self.assertEqual(s.mode, "bug")

    def test_mode_override(self):
        self.assertEqual(parse_focus("흐름", mode="refactor").mode, "refactor")


class IndexerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.tmp.name))
        self.idx = RepoIndex(str(self.repo), AgentConfig()).build()

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, file):
        return {(s.qualname, s.kind) for s in self.idx.symbols if s.file == file}

    def test_excluded_dirs(self):
        self.assertNotIn("build/gen.py", self.idx.files)

    def test_cpp(self):
        h = self.names("src/engine.h")
        self.assertIn(("Engine", "class"), h)
        self.assertIn(("Engine::count", "method"), h)
        self.assertNotIn(("Fake", "class"), h)
        cpp = self.names("src/engine.cpp")
        self.assertEqual(cpp, {("Engine::Engine", "method"), ("Engine::step", "method"),
                               ("update", "function"), ("runLoop", "function")})
        step = self.idx.by_name["step"][0]
        self.assertEqual((step.start, step.end), (6, 11))
        self.assertIn("update", step.calls)
        self.assertNotIn("fake", step.calls)
        cls = next(s for s in self.idx.by_name["Engine"] if s.is_class)
        self.assertEqual(cls.bases, ["Base"])

    def test_python(self):
        py = self.names("py/session.py")
        self.assertIn(("Session::connect", "method"), py)
        self.assertIn(("retry", "function"), py)
        ready = self.idx.by_name["ready"][0]
        self.assertEqual(ready.start, 12)  # 데코레이터 포함
        self.assertIn("retry", self.idx.by_name["connect"][0].calls)

    def test_go_receiver(self):
        go = self.names("server.go")
        self.assertIn(("Handle", "function"), go)
        self.assertIn(("helper", "function"), go)

    def test_masking_keeps_lines(self):
        syms = parse_c_like('/* a\n b */\nint f() {\n  return 1;\n}\n', "x.c")
        self.assertEqual([(s.name, s.start, s.end) for s in syms], [("f", 3, 5)])


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.tmp.name))
        self.cfg = AgentConfig()
        self.idx = RepoIndex(str(self.repo), self.cfg).build()
        self.r = Retriever(self.idx)

    def tearDown(self):
        self.tmp.cleanup()

    def titles(self, res, prio=None):
        return [it.title for it in res.items if prio is None or it.priority == prio]

    def test_class_pulls_out_of_line_methods(self):
        res = self.r.retrieve(parse_focus("class:Engine 구조"))
        self.assertIn("class Engine", self.titles(res, 0))
        members = self.titles(res, 2)
        self.assertIn("method Engine::step", members)
        self.assertIn("method Engine::Engine", members)
        self.assertNotIn("method Engine::count", members)  # 클래스 본문에 이미 포함

    def test_function_expands_callers_and_callees(self):
        res = self.r.retrieve(parse_focus("func:Engine::step 흐름"))
        self.assertEqual(self.titles(res, 0), ["method Engine::step"])
        rel = self.titles(res, 3)
        self.assertIn("function update", rel)   # callee
        self.assertIn("function runLoop", rel)  # caller

    def test_depth_zero(self):
        res = self.r.retrieve(parse_focus("func:step"), depth=0)
        self.assertEqual(self.titles(res, 3), [])

    def test_keyword_maps_to_enclosing_symbol(self):
        res = self.r.retrieve(parse_focus('keyword:"TIMEOUT"'))
        self.assertIn("method Session::connect", self.titles(res, 1))
        self.assertIn("'TIMEOUT' 등장 위치", self.titles(res, 1))  # 모듈 레벨 상수

    def test_fuzzy_and_unresolved(self):
        res = self.r.retrieve(parse_focus("class:Sesion"))
        self.assertIn("class Session", self.titles(res, 0))
        self.assertTrue(any("유사" in n for n in res.notes))
        res = self.r.retrieve(parse_focus("func:doesNotExistAnywhere"))
        self.assertEqual(res.unresolved, ["doesNotExistAnywhere"])
        self.assertIn("저장소 구조", self.titles(res, 4))

    def test_weak_identifier_only_if_exists(self):
        res = self.r.retrieve(parse_focus("helper 는 뭐하는 거야? banana"))
        self.assertIn("function helper", self.titles(res, 0))
        self.assertEqual(res.unresolved, [])

    def test_pack_splits_large(self):
        res = self.r.retrieve(parse_focus("class:Session class:Engine"))
        batches = pack(res.items, budget=600)
        self.assertGreater(len(batches), 1)
        for b in batches:
            self.assertLessEqual(sum(it.size() for it in b), 600 + max(it.size() for it in b))


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = str(make_repo(Path(self.tmp.name)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_call(self):
        llm = FakeLLM()
        agent = CodeAnalysisAgent(AgentConfig(), llm=llm)
        res = agent.analyze(self.repo, "`Engine::step` 버그 찾아줘")
        self.assertEqual(res.answer, "answer-1")
        user = llm.calls[0][1]["content"]
        self.assertIn("[주요 대상] method Engine::step", user)
        self.assertIn("결함 분석", user)
        self.assertIn(" 6| void Engine::step", user)

    def test_map_reduce(self):
        llm = FakeLLM()
        agent = CodeAnalysisAgent(AgentConfig(context_chars=450), llm=llm)
        res = agent.analyze(self.repo, "class:Session class:Engine 설명")
        self.assertGreater(len(res.batches), 1)
        self.assertEqual(len(llm.calls), len(res.batches) + 1)
        self.assertIn("부분 분석 메모", llm.calls[-1][1]["content"])

    def test_dry_run_no_llm(self):
        llm = FakeLLM()
        res = CodeAnalysisAgent(AgentConfig(), llm=llm).analyze(self.repo, "class:Engine", dry_run=True)
        self.assertEqual(llm.calls, [])
        self.assertEqual(res.answer, "")
        self.assertTrue(res.messages)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, body, dict(self.headers)))
        if self.path.endswith("/chat/completions"):
            out = {"choices": [{"message": {"content": "openai-ok"}}]}
        else:
            out = {"message": {"content": "ollama-ok"}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class LLMClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.srv.seen = []
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_openai(self):
        cfg = AgentConfig(base_url=self.url + "/v1", model="m", api_key="k",
                          extra_headers={"X-Team": "a"})
        self.assertEqual(LLMClient(cfg).chat([{"role": "user", "content": "hi"}]), "openai-ok")
        path, body, headers = self.srv.seen[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body["model"], "m")
        self.assertEqual(headers["Authorization"], "Bearer k")
        self.assertEqual(headers["X-Team"], "a")

    def test_ollama(self):
        cfg = AgentConfig(provider="ollama", base_url=self.url, model="qwen")
        self.assertEqual(LLMClient(cfg).chat([{"role": "user", "content": "hi"}]), "ollama-ok")
        self.assertEqual(self.srv.seen[-1][0], "/api/chat")


class ConfigTests(unittest.TestCase):
    def test_file_env_override(self):
        import os
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"model": "from-file", "context_chars": 1000, "unknown": 1}, f)
        os.environ["CODE_AGENT_CONTEXT_CHARS"] = "5000"
        try:
            cfg = AgentConfig.load(f.name, model="from-cli")
        finally:
            del os.environ["CODE_AGENT_CONTEXT_CHARS"]
            os.unlink(f.name)
        self.assertEqual(cfg.model, "from-cli")
        self.assertEqual(cfg.context_chars, 5000)


if __name__ == "__main__":
    unittest.main()
