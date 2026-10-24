"""V6.2 router quality regression: labelled corpus, discovery, and fail-closed routing."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.router import discover, route


DATA = Path(__file__).resolve().parent / "data"


def load_catalog():
    return json.loads((DATA / "router_catalog_v62.json").read_text(encoding="utf-8"))


def load_cases(split):
    cases = json.loads((DATA / "router_goldens_v62.json").read_text(encoding="utf-8"))
    return [case for case in cases if case["split"] == split]


class RouterQualityV62Tests(unittest.TestCase):
    def evaluate(self, split):
        catalog = load_catalog()
        rows = []
        for case in load_cases(split):
            task = {"objective": case["objective"], "role": "implementer",
                    "read": case.get("read", []), "write": case.get("write", []),
                    "skills": []}
            picked = [item["name"] for item in route(task, catalog, max_skills=2)]
            expected = set(case["expected"])
            top1 = (picked == []) if not expected else bool(picked) and picked[0] in expected
            rows.append((expected, picked, top1))
        negatives = [r for r in rows if not r[0]]
        positives = [r for r in rows if r[0]]
        top1 = sum(r[2] for r in rows) / len(rows)
        fpr = sum(1 for r in negatives if r[1]) / len(negatives) if negatives else 0.0
        fnr = sum(1 for r in positives if not (set(r[1]) & r[0])) / len(positives) if positives else 0.0
        tp = sum(len(set(r[1]) & r[0]) for r in rows)
        fp = sum(len(set(r[1]) - r[0]) for r in rows)
        fn = sum(len(r[0] - set(r[1])) for r in rows)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        return top1, fpr, fnr, precision, recall

    def test_eval_split_meets_quality_targets(self):
        top1, fpr, fnr, precision, recall = self.evaluate("eval")
        self.assertGreaterEqual(top1, 0.90, f"Top-1 {top1:.3f} < 0.90")
        self.assertLessEqual(fpr, 0.05, f"FPR {fpr:.3f} > 0.05")
        self.assertLessEqual(fnr, 0.10, f"FNR {fnr:.3f} > 0.10")
        self.assertGreaterEqual(precision, 0.80, f"Precision {precision:.3f} < 0.80")
        self.assertGreaterEqual(recall, 0.80, f"Recall {recall:.3f} < 0.80")

    def test_holdout_and_blind_splits_do_not_regress(self):
        for split in ("dev", "holdout", "blind"):
            with self.subTest(split=split):
                top1, fpr, *_ = self.evaluate(split)
                self.assertGreaterEqual(top1, 0.90)
                self.assertLessEqual(fpr, 0.05)

    def test_brand_plural_and_imperative_verb_handling(self):
        catalog = [
            {"name": "devops-docker", "description": "Deploy with Docker, Kubernetes and CI pipelines", "path": "/x"},
            {"name": "research-scout", "description": "Research options and benchmark implementations", "path": "/y"},
            {"name": "security-audit", "description": "Audit authentication, secrets and XSS risks", "path": "/z"},
        ]
        cases = [
            ("Fix the GitHub Actions workflow", ["devops-docker"]),
            ("Rotate leaked API keys", ["security-audit"]),
            ("Benchmark message queue implementations", ["research-scout"]),
            ("Delete the temporary benchmark results folder", []),
        ]
        for objective, expected in cases:
            with self.subTest(objective=objective):
                picked = [x["name"] for x in route(
                    {"objective": objective, "role": "implementer", "read": [], "write": [], "skills": []},
                    catalog)]
                self.assertEqual(picked, expected)


class RouterDiscoveryV62Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        (self.repo / "sub" / "project").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        self.home = self.base / "home"
        self.home.mkdir()

    def write_skill(self, root, name, description):
        path = root / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n", encoding="utf-8")
        return path

    def test_multiline_ancestor_home_and_symlink_discovery(self):
        project = self.repo / "sub" / "project"
        self.write_skill(project / ".agents/skills", "alpha", ">\n  Build backend API\n  validation services.")
        self.write_skill(self.repo / ".agents/skills", "beta", '"Create React components"')
        self.write_skill(self.home / ".agents/skills", "gamma", "Write pytest tests")
        real = self.home / "real" / "delta"
        self.write_skill(self.home / "real", "delta", "Deploy docker containers")
        (self.home / ".claude" / "skills").mkdir(parents=True)
        os.symlink(real, self.home / ".claude" / "skills" / "delta")
        found = {x["name"]: x for x in discover(project, home=self.home)}
        self.assertEqual(found["alpha"]["description"], "Build backend API validation services.")
        self.assertEqual(found["beta"]["description"], "Create React components")
        self.assertEqual(found["gamma"]["path"], str((self.home / ".agents/skills/gamma/SKILL.md").resolve()))
        self.assertEqual(found["delta"]["path"], str((real / "SKILL.md").resolve()))

    def test_duplicate_names_keep_first_and_invalid_metadata_falls_back(self):
        project = self.repo / "sub" / "project"
        self.write_skill(project / ".agents/skills", "same", "First description")
        self.write_skill(self.repo / ".agents/skills", "same", "Second description")
        bad = project / ".agents/skills" / "bad name" / "SKILL.md"
        bad.parent.mkdir(parents=True)
        bad.write_text("---\nname: 'bad name'\ndescription: ignored\n---\n", encoding="utf-8")
        found = discover(project, home=self.home)
        names = [x["name"] for x in found]
        self.assertEqual(names.count("same"), 1)
        self.assertIn("bad name", names)
        self.assertEqual(next(x["description"] for x in found if x["name"] == "same"), "First description")

    def test_symlink_escaping_project_and_home_is_rejected(self):
        project = self.repo / "sub" / "project"
        outside = self.base / "outside" / "evil"
        self.write_skill(outside.parent, "evil", "Backend API endpoints")
        (project / ".agents" / "skills").mkdir(parents=True)
        os.symlink(outside, project / ".agents/skills/evil")
        self.assertNotIn("evil", [x["name"] for x in discover(project, home=self.home)])


if __name__ == "__main__":
    unittest.main()
