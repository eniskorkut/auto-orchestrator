"""V6.4 Router evaluation metrics and domain ontology regression tests."""
import json
import unittest
from pathlib import Path
from tools.router import route
from tools.router_eval import evaluate, EvaluationError


DATA=Path(__file__).parent/"data"
CATALOG=json.loads((DATA/"router_catalog_v62.json").read_text(encoding="utf-8"))
CASES=json.loads((DATA/"router_goldens_v62.json").read_text(encoding="utf-8"))


class RouterDiagnostics(unittest.TestCase):
    def test_reports_abstention_separately_from_unrelated_selections(self):
        cases=[
            {"split":"new","objective":"Implement FastAPI backend API endpoint","expected":["backend-api"]},
            {"split":"new","objective":"Translate this poem","expected":[]},
            {"split":"new","objective":"Implement circuit breaking for payment API client","expected":["backend-api"]},
        ]
        result=evaluate(cases,CATALOG)["splits"]["new"]
        self.assertEqual(result["n"],3)
        self.assertEqual(result["negative_n"],1)
        self.assertIsNotNone(result["false_negative_abstention_rate"])
        self.assertIn("unrelated_skill_selections",result)
        self.assertIn("negative_false_positive_rate",result)

    def test_complete_frozen_corpus_can_be_audited_by_split(self):
        result=evaluate(CASES,CATALOG)
        self.assertEqual(sum(x["n"] for x in result["splits"].values()),227)
        self.assertEqual(result["splits"]["blind3"]["n"],30)
        self.assertIn("negative FPR",result["note"])

    def test_domain_specific_vocabulary_without_generic_override(self):
        for objective,good in (
            ("Add circuit breaking to a payment API client","backend-api"),
            ("Implement outbox pattern in backend service","backend-api"),
            ("Build semantic search using vector embeddings","rag-llm"),
        ):
            with self.subTest(objective=objective):
                selections=[s["name"] for s in route({
                    "objective":objective,"role":"implementer","read":[],
                    "write":[],"skills":[]},CATALOG)]
                self.assertIn(good,selections)
        result=route({"objective":"Write a limerick about deployment","role":"implementer",
                      "read":[],"write":[],"skills":[]},CATALOG)
        self.assertEqual(result,[])

    def test_rejects_invalid_corpus(self):
        with self.assertRaises(EvaluationError):
            evaluate([{"objective":"Any task","expected":"backend-api"}],CATALOG)


if __name__=="__main__":
    unittest.main()
