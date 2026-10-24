"""V6.2 deterministic router and opt-in Supervisor review-gate regressions."""
import hashlib
import json
import tempfile
import unittest

from tools.router import route, brief
from tools.review_gate import result_digest, validate_review, ReviewError
from tools.supervisor import Supervisor, SupervisorError


CATALOG = [
    {"name": "animation-vocabulary", "description": "Create UI motion animations and interactive animation vocabulary", "path": "/skills/animation"},
    {"name": "break-ui", "description": "Break down React interface UI components and responsive navbar layouts", "path": "/skills/ui"},
    {"name": "backend-api", "description": "Build Python FastAPI backend API endpoints and server routes", "path": "/skills/backend"},
    {"name": "postgres-database", "description": "Create PostgreSQL database schemas, SQL migrations and indexes", "path": "/skills/postgres"},
    {"name": "security-auth", "description": "Audit authentication authorization security vulnerabilities and secrets", "path": "/skills/security"},
    {"name": "pytest-testing", "description": "Write pytest tests regression testing coverage for Python modules", "path": "/skills/testing"},
    {"name": "helm-deploy", "description": "Deploy kubernetes helm services using CI pipelines", "path": "/skills/deploy"},
    {"name": "pandas-analysis", "description": "Analyze pandas CSV datasets and dataframes for analytics", "path": "/skills/data"},
    {"name": "docs-writer", "description": "Write documentation README tutorials and changelog", "path": "/skills/docs"},
    {"name": "generic-helper", "description": "Help implement projects and manage code tasks", "path": "/skills/generic"},
]


class RouterV62Tests(unittest.TestCase):
    def select(self, objective, read=None, write=None, explicit=None, max_skills=2):
        task = {"objective": objective, "role": "implementer",
                "read": read or [], "write": write or [], "skills": explicit or []}
        return [x["name"] for x in route(task, CATALOG, max_skills=max_skills)]

    def test_backend_does_not_route_to_animation_or_break_ui(self):
        results = self.select("Implement FastAPI backend API endpoint and server retries",
                              read=["src/api/routes.py"], write=["src/api/retry.py"])
        self.assertIn("backend-api", results)
        self.assertNotIn("animation-vocabulary", results)
        self.assertNotIn("break-ui", results)

    def test_routing_goldens(self):
        scenarios = [
            ("Build responsive React UI navbar components", "break-ui"),
            ("Create animation motion and transitions", "animation-vocabulary"),
            ("Secure authentication authorization against vulnerabilities", "security-auth"),
            ("Add Python pytest regression coverage", "pytest-testing"),
            ("Deploy service with kubernetes helm", "helm-deploy"),
            ("Analyze pandas CSV datasets", "pandas-analysis"),
            ("Update README documentation tutorial", "docs-writer"),
            ("Create PostgreSQL database SQL migrations", "postgres-database"),
            ("Implement Python FastAPI endpoint", "backend-api"),
        ]
        for objective, expected in scenarios:
            with self.subTest(objective=objective):
                self.assertIn(expected, self.select(objective))

    def test_no_match_abstains_even_with_generic_skill(self):
        self.assertEqual(self.select("Refactor collection processing for reliability"), [])
        self.assertEqual(self.select("Translate this short story"), [])

    def test_explicit_selection_and_max_skills(self):
        self.assertEqual(self.select("Backend endpoint changes", explicit=["animation-vocabulary"],
                                     max_skills=1), ["animation-vocabulary"])
        self.assertEqual(self.select("Fix backend endpoint", max_skills=0), [])
        with self.assertRaises(SupervisorError):
            route({"objective": "API backend", "read": [], "write": [], "skills": "invalid"}, CATALOG)
        with self.assertRaises(SupervisorError):
            self.select("API backend", max_skills=7)

    def test_scope_words_cannot_override_clear_domain_mismatch(self):
        result = self.select("Implement backend API endpoint", read=["assets/animation/layout.tsx"])
        self.assertNotIn("animation-vocabulary", result)
        self.assertNotIn("break-ui", result)


def result():
    return {"summary": "Implemented and verified", "changed_files": ["src/api.py"],
            "tests": [{"name": "pytest", "status": "passed"}], "findings": []}


def review(task_id="backend", attempt=1, payload=None):
    payload = result() if payload is None else payload
    return {"version": 1, "task_id": task_id, "attempt": attempt,
            "result_sha256": result_digest(payload), "verdict": "approved",
            "checks": [{"name": "pytest", "status": "passed"}], "findings": []}


def plan(required):
    return {"native_supervision": False, "require_review": required, "max_parallel": 1,
            "tasks": [{"id": "backend", "role": "implementer",
                       "objective": "Implement backend", "read_scope": [],
                       "write_scope": ["src"], "max_retries": 0}]}


class SupervisorReviewV62Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Supervisor(self.tmp.name)
        self.addCleanup(self.s.close)

    def claim(self, required=True):
        self.s.init("run", plan(required))
        return self.s.claim("run")["claimed"][0]["lease"]

    def test_opt_in_review_required_before_completion(self):
        lease = self.claim()
        with self.assertRaisesRegex(SupervisorError, "Quality review rejected"):
            self.s.change("run", "backend", lease, "completed", result=result())
        self.assertEqual(self.s.status("run")["tasks"][0]["state"], "running")
        self.assertEqual(self.s.change("run", "backend", lease, "completed",
                                       result=result(), review=review())["state"], "completed")
        self.assertTrue(self.s.status("run")["require_review"])

    def test_old_plans_remain_compatible(self):
        lease = self.claim(False)
        self.assertEqual(self.s.change("run", "backend", lease, "completed",
                                       result=result())["state"], "completed")

    def test_stale_attempt_or_modified_result_rejected(self):
        lease = self.claim()
        bad = review()
        bad["attempt"] = 2
        with self.assertRaisesRegex(SupervisorError, "Quality review rejected"):
            self.s.change("run", "backend", lease, "completed", result=result(), review=bad)
        changed = result()
        changed["summary"] += " modified"
        with self.assertRaisesRegex(SupervisorError, "Review digest"):
            self.s.change("run", "backend", lease, "completed", result=changed, review=review())

    def test_blockers_or_failed_tests_rejected(self):
        lease = self.claim()
        with_blocker = review()
        with_blocker["findings"] = [{"severity": "high", "status": "open",
                                   "summary": "Unresolved auth bypass"}]
        with self.assertRaisesRegex(SupervisorError, "Unresolved"):
            self.s.change("run", "backend", lease, "completed", result=result(), review=with_blocker)
        skipped = result()
        skipped["tests"] = [{"name": "pytest", "status": "skipped"}]
        with self.assertRaisesRegex(SupervisorError, "passing tests"):
            self.s.change("run", "backend", lease, "completed", result=skipped,
                          review=review(payload=skipped))

    def test_mismatched_check_or_scope_rejected(self):
        lease = self.claim()
        bad = review()
        bad["checks"][0]["name"] = "unrelated"
        with self.assertRaisesRegex(SupervisorError, "checks differ"):
            self.s.change("run", "backend", lease, "completed", result=result(), review=bad)
        outside = result()
        outside["changed_files"] = ["README.md"]
        with self.assertRaisesRegex(SupervisorError, "write scope"):
            self.s.change("run", "backend", lease, "completed", result=outside,
                          review=review(payload=outside))

    def test_review_plan_requires_boolean(self):
        with self.assertRaisesRegex(SupervisorError, "require_review"):
            self.s.init("broken", plan("true"))

    def test_review_no_native_stop_bypass(self):
        supervised = plan(True)
        supervised["native_supervision"] = True
        self.s.init("supervised", supervised)
        lease = self.s.claim("supervised")["claimed"][0]["lease"]
        with self.assertRaisesRegex(SupervisorError, "Native termination"):
            self.s.change("supervised", "backend", lease, "completed", result=result(), review=review())


if __name__ == "__main__":
    unittest.main()
