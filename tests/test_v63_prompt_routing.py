"""V6.3 native prompt routing contract and adaptive delegation advice."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tools.router import select_prompt, delegation_decision
from tools.supervisor import SupervisorError
from tools.perf import analyze

ROUTER = Path(__file__).resolve().parent.parent / "tools" / "router.py"
SKILLS = [
    {"name": "backend-api", "description": "Build backend API endpoints with FastAPI"},
    {"name": "ui-animation", "description": "Animate frontend UI layout transitions"},
    {"name": "test-automation", "description": "Implement unit integration tests and pytest coverage"},
]


class PromptRouterV63(unittest.TestCase):
    def test_native_backend_skill_selected_without_frontend(self):
        x = select_prompt({"objective": "Implement FastAPI backend API endpoint", "skills": SKILLS})
        self.assertIn("backend-api", x["selected"])
        self.assertNotIn("ui-animation", x["selected"])
        self.assertEqual(x["delegation"]["max_agents"], 1)

    def test_no_match_abstains(self):
        x = select_prompt({"objective": "Translate a short recipe", "skills": SKILLS})
        self.assertEqual(x["selected"], [])
        self.assertTrue(x["abstained"])

    def test_small_tasks_recommend_one_agent(self):
        for objective in ("Add a docstring", "Fix typo in README", "Run pytest", "Improve this endpoint"):
            with self.subTest(objective=objective):
                self.assertEqual(delegation_decision(objective)["max_agents"], 1)

    def test_explicit_parallel_tasks_bounded(self):
        x = delegation_decision("These independent modules can be parallel:\n"
                                "1. Implement API\n2. Create frontend\n3. Write tests\n4. Review docs")
        self.assertEqual(x["mode"], "consider-parallel")
        self.assertEqual(x["max_agents"], 3)
        self.assertEqual(delegation_decision("parallelize this typo fix")["max_agents"], 1)

    def test_inline_numbered_independent_tasks(self):
        x = delegation_decision("Üç bağımsız modül geliştir: 1. backend API "
                                "2. frontend bileşen 3. test otomasyonu. Bağımsız çalışabilirler.")
        self.assertEqual(x["mode"], "consider-parallel")
        self.assertEqual(x["max_agents"], 3)

    def test_turkish_backend_prompt_excludes_platform_skill(self):
        catalog = SKILLS + [
            {"name": "write-swift", "description": "Write modern Swift, Swift Testing, API design and performance"},
            {"name": "mobile-native", "description": "Build mobile native iOS Swift interfaces"},
        ]
        x = select_prompt({"objective": "API endpoint'ine doğrulama ekle ve testini yaz.",
                           "skills": catalog})
        self.assertIn("backend-api", x["selected"])
        self.assertNotIn("write-swift", x["selected"])
        self.assertNotIn("mobile-native", x["selected"])

    def test_web_frontend_prompt_excludes_mobile_native(self):
        catalog = [
            {"name": "frontend-react", "description": "Build React components with CSS and responsive layouts"},
            {"name": "mobile-native", "description": "Build mobile native iOS Swift interfaces"},
            {"name": "write-swift", "description": "Write modern Swift, Swift Testing, API design"},
        ]
        x = select_prompt({"objective": "Build a responsive React component with CSS", "skills": catalog})
        self.assertEqual(x["selected"][0], "frontend-react")
        self.assertNotIn("mobile-native", x["selected"])
        self.assertNotIn("write-swift", x["selected"])

    def test_turkish_mobile_web_prompt_excludes_native_swift_skill(self):
        catalog = [
            {"name": "frontend-react", "description": "Build React responsive UI layouts and CSS"},
            {"name": "mobile-swift", "description": "Build mobile native iOS Swift interfaces"},
        ]
        x = select_prompt({"objective": "Mobil menüdeki taşmayı düzelt", "skills": catalog})
        self.assertIn("frontend-react", x["selected"])
        self.assertNotIn("mobile-swift", x["selected"])

    def test_route_metadata_does_not_break_perf_analysis(self):
        events = [
            {"event": "start", "time": 1.0, "platform": "opencode"},
            {"event": "route", "time": 1.2, "source": "native-prompt-hook",
             "selected_count": 1, "delegation": "single", "max_agents": 1},
            {"event": "finish", "time": 2.0, "status": "success", "verified": True},
        ]
        result = analyze("route-run", events)
        self.assertTrue(result.verified)
        self.assertNotEqual(result.reason, "Unknown event")
        self.assertEqual(result.elapsed_seconds, 1.0)

    def test_bad_inputs_fail_closed(self):
        for payload in (
            {"objective": None, "skills": []},
            {"objective": "API", "skills": ["not-a-skill"]},
            {"objective": "API", "skills": [{"name": "bad/../../", "description": "API"}]},
            {"objective": "A" * 12001, "skills": []},
        ):
            with self.subTest(payload=str(payload)[:50]):
                with self.assertRaises(SupervisorError):
                    select_prompt(payload)

    def test_cli_stdin_roundtrip_no_prompt_echo(self):
        raw = "Implement FastAPI backend API endpoint. SENSITIVE_DO_NOT_LOG"
        run = subprocess.run([sys.executable, str(ROUTER), "select"],
                             input=json.dumps({"objective": raw, "skills": SKILLS}),
                             text=True, capture_output=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("backend-api", json.loads(run.stdout)["selected"])
        self.assertNotIn("SENSITIVE_DO_NOT_LOG", run.stdout)

    def test_cli_rejects_malformed_payload(self):
        run = subprocess.run([sys.executable, str(ROUTER), "select"], input="{bad",
                             text=True, capture_output=True, timeout=5)
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn("Traceback", run.stderr)


if __name__ == "__main__":
    unittest.main()
