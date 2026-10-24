"""V6.3 native prompt routing contract and adaptive delegation advice."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tools.router import select_prompt, delegation_decision
from tools.supervisor import SupervisorError

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
