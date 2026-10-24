#!/usr/bin/env python3
"""Evaluate deterministic Router against a labelled, reproducible skill corpus.

No prompt or user work is logged; the tool reads only caller-selected fixtures.
Do not tune against a split and then call it an unseen holdout.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

try:
    from tools.router import route
except ModuleNotFoundError:
    from router import route


class EvaluationError(ValueError):
    pass


def evaluate(cases: list, catalog: list, max_skills: int = 2) -> dict:
    if not isinstance(cases, list) or not cases or not isinstance(catalog, list):
        raise EvaluationError("Nonempty case list and skill catalog required")
    by_split = defaultdict(list)
    for row in cases:
        if not isinstance(row, dict) or not isinstance(row.get("objective"), str):
            raise EvaluationError("Each case needs a string objective")
        expected = row.get("expected")
        if (not isinstance(expected, list) or
                any(not isinstance(x, str) for x in expected)):
            raise EvaluationError("Each case needs an expected skill list")
        label = row.get("split", "unspecified")
        if not isinstance(label, str) or not label or len(label) > 50:
            raise EvaluationError("Invalid split")
        task = {"objective": row["objective"], "role": "implementer",
                "read": row.get("read", []), "write": row.get("write", []),
                "skills": []}
        picked = [item["name"] for item in route(task, catalog, max_skills)]
        target = set(expected)
        by_split[label].append((target, picked))
    groups = {}
    for split, rows in sorted(by_split.items()):
        tp = sum(len(set(picks) & expected) for expected, picks in rows)
        fp = sum(len(set(picks) - expected) for expected, picks in rows)
        fn = sum(len(expected - set(picks)) for expected, picks in rows)
        negatives = [(exp,picks) for exp,picks in rows if not exp]
        positives = [(exp,picks) for exp,picks in rows if exp]
        top1 = sum((not picks if not exp else bool(picks) and picks[0] in exp)
                   for exp,picks in rows) / len(rows)
        groups[split] = {
            "n": len(rows),
            "positive_n": len(positives), "negative_n": len(negatives),
            "top1_accuracy": round(top1, 4),
            "precision": round(tp/(tp+fp), 4) if tp+fp else None,
            "recall": round(tp/(tp+fn), 4) if tp+fn else None,
            # FPR applies ONLY to negatives. Precision includes incorrect
            # selections on all positive/multi-label tasks too.
            "negative_false_positive_rate":
                round(sum(bool(picks) for _,picks in negatives)/len(negatives), 4)
                if negatives else None,
            "false_negative_abstention_rate":
                round(sum(not picks for _,picks in positives)/len(positives), 4)
                if positives else None,
            "unrelated_skill_selections": fp,
            "missing_expected_skill_selections": fn,
            "abstentions": sum(not picks for _,picks in rows),
        }
    return {"splits": groups, "note":
            "These are labelled corpus metrics; not production success rates. "
            "The negative FPR does not count unrelated skills on positive cases."}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cases",required=True,type=Path)
    p.add_argument("--catalog",required=True,type=Path)
    p.add_argument("--max-skills",type=int,default=2)
    a=p.parse_args(argv)
    try:
        cases=json.loads(a.cases.read_text(encoding="utf-8"))
        catalog=json.loads(a.catalog.read_text(encoding="utf-8"))
        print(json.dumps(evaluate(cases,catalog,a.max_skills),indent=2))
    except (OSError,ValueError,TypeError,KeyError) as exc:
        p.exit(1,"ROUTER EVAL ERROR: "+str(exc)+"\n")


if __name__=="__main__":
    main()
