from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.grill_me import GrillError, GrillMeEngine
from tools.project_memory import ProjectMemory
from tools.requirements_contract import load_current


class GrillMeChangeFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _start(self, engine, request="Kullanıcı giriş sistemini geliştir"):
        return engine.start(request, origin_platform="codex", mode="auto")

    def _answer_next(self, engine, interview_id, option):
        question = engine.next_question(interview_id)
        self.assertIsNotNone(question)
        return engine.answer(question["question_id"], option=option)

    def test_jwt_to_cookie_invalidates_branch_and_never_asks_jwt_followup(self):
        with GrillMeEngine(self.root) as engine:
            started = self._start(engine)
            auth = started["question"]
            self.assertEqual(auth["question_key"], "auth_session")

            answered = engine.answer(auth["question_id"], option="jwt")
            jwt_follow = answered["next"]
            self.assertEqual(jwt_follow["question_key"], "jwt_refresh")
            engine.answer(jwt_follow["question_id"], option="refresh")

            back = engine.back(auth["question_id"], reason="reconsider auth")
            self.assertEqual(back["question"]["status"], "open")
            self.assertEqual(back["question"]["previous_answer"]["option"], "jwt")
            self.assertTrue(any(
                event["event"] == "back"
                for event in back["question"]["history"]))
            self.assertEqual(back["next"]["question_key"], "auth_session")

            changed = engine.answer(
                auth["question_id"], option="cookie",
                reason="cookie session is safer")
            self.assertTrue(changed["changed"])
            self.assertEqual(changed["next"]["question_key"], "session_expiry")
            self.assertNotEqual(changed["next"]["question_key"], "jwt_refresh")

            invalidated_questions = {
                item["question_key"]: item
                for item in changed["invalidated_questions"]}
            self.assertIn("jwt_refresh", invalidated_questions)
            self.assertEqual(
                invalidated_questions["jwt_refresh"]["status"], "invalidated")
            self.assertIn(
                "no longer applies",
                invalidated_questions["jwt_refresh"]["invalidated_reason"])

            summary = engine.summary()
            self.assertFalse(summary["ready"])
            self.assertIn(
                "jwt_refresh",
                {item["question_key"] for item in summary["invalidated_questions"]})
            self.assertTrue(all(
                item["status"] != "invalidated"
                for item in summary["decisions"]))
            active_topics = {item["topic"] for item in summary["active_decisions"]}
            self.assertEqual(active_topics, {"auth_session"})
            self.assertIn("cookie", summary["active_decisions"][0]["decision"])
            invalidated_topics = {
                item["topic"] for item in summary["invalidated_decisions"]}
            self.assertEqual(invalidated_topics,
                             {"auth_session", "jwt_refresh"})
            old_auth = next(
                item for item in summary["invalidated_decisions"]
                if item["topic"] == "auth_session")
            self.assertEqual(old_auth["confirmed_by"], "user")
            self.assertIsNotNone(old_auth["confirmed_at"])
            # Old answer history survives on the changed question.
            changed_question = engine._question(auth["question_id"])
            self.assertEqual(changed_question["selected_option"], "cookie")
            self.assertEqual(
                changed_question["previous_answer"]["option"], "cookie")
            history = [
                (event["event"], event.get("previous_option"),
                 event.get("new_option"), event.get("reason"))
                for event in summary["history"]
                if event.get("question_key") == "auth_session"]
            self.assertIn(("answer", None, "jwt", None), history)
            self.assertIn(("back", "jwt", None, "reconsider auth"), history)
            self.assertIn(
                ("answer_changed", "jwt", "cookie",
                 "cookie session is safer"), history)

    def test_cookie_branch_question_is_reopened_when_switching_back(self):
        with GrillMeEngine(self.root) as engine:
            started = self._start(engine, "Oturum yönetimi ekle")
            interview_id = started["interview"]["interview_id"]
            auth = started["question"]
            cookie = engine.answer(auth["question_id"], option="cookie")
            expiry = cookie["next"]
            self.assertEqual(expiry["question_key"], "session_expiry")
            engine.answer(expiry["question_id"], option="idle30")

            engine.back(auth["question_id"])
            jwt = engine.answer(auth["question_id"], option="jwt")
            self.assertEqual(jwt["next"]["question_key"], "jwt_refresh")
            self.assertIn(
                "session_expiry",
                {item["question_key"] for item in jwt["invalidated_questions"]})

            engine.answer(jwt["next"]["question_id"], option="refresh")
            engine.back(auth["question_id"])
            cookie_again = engine.answer(auth["question_id"], option="cookie")
            self.assertEqual(cookie_again["next"]["question_key"],
                             "session_expiry")
            reopened = {
                item["question_key"]: item
                for item in cookie_again["reopened_questions"]}
            self.assertIn("session_expiry", reopened)
            self.assertEqual(
                reopened["session_expiry"]["previous_answer"]["option"], "idle30")
            summary = engine.summary()
            self.assertIn(
                "session_expiry",
                {item["question_key"] for item in summary["reopened_questions"]})
            self.assertFalse(summary["ready"])
            self.assertNotIn(
                "jwt_refresh",
                {item["question_key"] for item in summary["reopened_questions"]})

    def test_unrelated_decisions_and_requirements_survive(self):
        with ProjectMemory(self.root) as memory:
            memory.add_decision(
                topic="manual_topic", decision="keep this decision",
                source="user", source_ref="manual:seed", status="confirmed",
                confirmed_by="user")
            memory.add_requirement(
                "keep this requirement", category="confirmed",
                status="confirmed", source="user", source_ref="manual:seed")
        with GrillMeEngine(self.root) as engine:
            started = self._start(
                engine, "Kullanıcı girişi ve veritabanı migrasyonu ekle")
            interview_id = started["interview"]["interview_id"]
            auth = started["question"]
            engine.answer(auth["question_id"], option="jwt")
            self._answer_next(engine, interview_id, "refresh")
            self._answer_next(engine, interview_id, "postgres")
            self._answer_next(engine, interview_id, "forward")
            engine.back(auth["question_id"])
            changed = engine.answer(auth["question_id"], option="cookie")
            self.assertTrue(changed["changed"])
            invalidated_topics = {
                item["topic"] for item in engine.summary()["invalidated_decisions"]}
            self.assertEqual(invalidated_topics,
                             {"auth_session", "jwt_refresh"})
        with ProjectMemory(self.root) as memory:
            active = {item["topic"]: item["status"]
                      for item in memory.list_decisions(limit=500)
                      if item["status"] == "confirmed"}
            self.assertEqual(active.get("manual_topic"), "confirmed")
            self.assertEqual(active.get("database"), "confirmed")
            self.assertEqual(active.get("db_rollback"), "confirmed")
            manual = [item for item in memory.list_requirements(limit=500)
                      if item["source_ref"] == "manual:seed"]
            self.assertEqual(manual[0]["status"], "confirmed")

    def test_requirement_rows_owned_by_the_changed_question_are_invalidated(self):
        with GrillMeEngine(self.root) as engine:
            started = self._start(engine, "Oturum yönetimi ekle")
            auth = started["question"]
            # A skipped question creates an unresolved requirement keyed to it.
            engine.skip(auth["question_id"])
            resumed = engine.resume()
            engine.answer(resumed["question"]["question_id"], option="jwt")
            with ProjectMemory(self.root) as memory:
                before = [item for item in memory.list_requirements(limit=500)
                          if item["source_ref"] == f"question:{auth['question_id']}"]
                self.assertEqual(before[0]["status"], "confirmed")
                decision = next(
                    item for item in memory.list_decisions(topic="auth_session")
                    if item["source_ref"] == f"question:{auth['question_id']}")
                memory.add_requirement(
                    "JWT must stay", category="confirmed", status="confirmed",
                    source="user",
                    source_ref=f"decision:{decision['decision_id']}")
            engine.back(auth["question_id"])
            engine.answer(auth["question_id"], option="cookie")
        with ProjectMemory(self.root) as memory:
            rows = {
                item["source_ref"]: item["status"]
                for item in memory.list_requirements(limit=500)
                if item["source_ref"] in (
                    f"question:{auth['question_id']}",
                    f"decision:{decision['decision_id']}")}
            self.assertEqual(rows[f"question:{auth['question_id']}"],
                             "invalidated")
            self.assertEqual(rows[f"decision:{decision['decision_id']}"],
                             "invalidated")

    def test_approve_is_blocked_after_invalidation_until_branch_is_answered(self):
        with GrillMeEngine(self.root) as engine:
            started = self._start(engine, "Oturum yönetimi ekle")
            interview_id = started["interview"]["interview_id"]
            auth = started["question"]
            first = engine.answer(auth["question_id"], option="jwt")
            engine.answer(first["next"]["question_id"], option="refresh")
            engine.back(auth["question_id"])
            changed = engine.answer(auth["question_id"], option="cookie")
            self.assertEqual(changed["next"]["question_key"], "session_expiry")
            self.assertFalse(engine.summary()["ready"])
            with self.assertRaises(GrillError):
                engine.approve()
            engine.skip(changed["next"]["question_id"])
            self.assertFalse(engine.summary()["ready"])
            with self.assertRaises(GrillError):
                engine.approve()
            # The skipped branch question is reopened by resume, not lost.
            resumed = engine.resume(interview_id)
            self.assertEqual(resumed["question"]["question_key"], "session_expiry")
            engine.answer(resumed["question"]["question_id"], option="idle30")
            self.assertTrue(engine.summary()["ready"])
            self.assertTrue(engine.approve()["approved"])

    def test_repeated_identical_answer_after_back_is_idempotent(self):
        with GrillMeEngine(self.root) as engine:
            started = self._start(engine, "Oturum yönetimi ekle")
            auth = started["question"]
            engine.answer(auth["question_id"], option="jwt")
            with ProjectMemory(self.root) as memory:
                first = [item for item in memory.list_decisions(topic="auth_session")
                         if item["source_ref"] == f"question:{auth['question_id']}"][0]
                memory.add_requirement(
                    "auth decision must stay explicit",
                    category="confirmed", status="confirmed", source="user",
                    source_ref=f"question:{auth['question_id']}")
            engine.back(auth["question_id"])
            with ProjectMemory(self.root) as memory:
                requirement = [item for item in memory.list_requirements(limit=500)
                               if item["source_ref"] ==
                               f"question:{auth['question_id']}"][0]
                self.assertEqual(requirement["status"], "invalidated")
            repeated = engine.answer(auth["question_id"], option="jwt")
            self.assertFalse(repeated["changed"])
            self.assertNotIn("invalidated_decisions", repeated)
            self.assertEqual(repeated["next"]["question_key"], "jwt_refresh")
            with ProjectMemory(self.root) as memory:
                current = [item for item in memory.list_decisions(topic="auth_session")
                           if item["source_ref"] == f"question:{auth['question_id']}"]
                self.assertEqual(len(current), 1)
                self.assertEqual(current[0]["decision_id"], first["decision_id"])
                self.assertEqual(current[0]["status"], "confirmed")
                requirement = [item for item in memory.list_requirements(limit=500)
                               if item["source_ref"] ==
                               f"question:{auth['question_id']}"][0]
                self.assertEqual(requirement["status"], "confirmed")
            summary = engine.summary()
            self.assertEqual(summary["invalidated_decisions"], [])
            self.assertEqual(summary["invalidated_questions"], [])
            # Repeating the exact answer without back is also idempotent.
            direct = engine.answer(auth["question_id"], option="jwt")
            self.assertFalse(direct["changed"])
            with ProjectMemory(self.root) as memory:
                current = [item for item in memory.list_decisions(topic="auth_session")
                           if item["source_ref"] == f"question:{auth['question_id']}"]
                self.assertEqual(len(current), 1)
                self.assertEqual(current[0]["status"], "confirmed")

    def test_change_reason_is_bounded_and_recorded_on_invalidations(self):
        with GrillMeEngine(self.root) as engine:
            started = self._start(engine, "Oturum yönetimi ekle")
            auth = started["question"]
            engine.answer(auth["question_id"], option="jwt")
            engine.back(auth["question_id"])
            with self.assertRaises(GrillError):
                engine.answer(auth["question_id"], option="cookie",
                              change_reason="x" * 501)
            changed = engine.answer(
                auth["question_id"], option="cookie",
                change_reason="cookie rotation policy changed")
            self.assertTrue(changed["changed"])
            with ProjectMemory(self.root) as memory:
                audit = memory.list_grill_audit(
                    started["interview"]["interview_id"], limit=100)
            change_events = [item for item in audit
                             if item["event"] == "answer_changed"]
            self.assertEqual(change_events[0]["reason"],
                             "cookie rotation policy changed")
            changed_at = change_events[0]["created_at"]
            invalidations = [item for item in audit
                             if item["event"] in (
                                 "decision_invalidated", "question_invalidated")
                             and item["created_at"] >= changed_at]
            self.assertTrue(invalidations)
            self.assertTrue(all(
                "cookie rotation policy changed" in (item["reason"] or "")
                for item in invalidations))

    def test_direct_reanswer_without_back_invalidates_the_previous_answer(self):
        with GrillMeEngine(self.root) as engine:
            started = self._start(engine, "Oturum yönetimi ekle")
            auth = started["question"]
            engine.answer(auth["question_id"], option="jwt")
            changed = engine.answer(auth["question_id"], option="cookie")
            self.assertTrue(changed["changed"])
            self.assertEqual(changed["next"]["question_key"], "session_expiry")
            summary = engine.summary()
            self.assertNotIn(
                "jwt",
                " ".join(item["decision"].casefold()
                         for item in summary["active_decisions"]))
            self.assertEqual(
                {item["topic"] for item in summary["invalidated_decisions"]},
                {"auth_session"})

    def test_skipped_generic_dependent_is_reopened_and_reported(self):
        with GrillMeEngine(self.root) as engine:
            started = self._start(engine, "Dosya paylaşımı ekle")
            interview_id = started["interview"]["interview_id"]
            access = started["question"]
            first = engine.answer(access["question_id"], option="owner")
            dependent = first["next"]
            self.assertEqual(dependent["question_key"], "file_delete")
            engine.skip(dependent["question_id"])
            engine.back(access["question_id"])
            changed = engine.answer(access["question_id"], option="team")
            self.assertTrue(changed["changed"])
            reopened = {
                item["question_key"]: item
                for item in changed["reopened_questions"]}
            self.assertIn("file_delete", reopened)
            self.assertIsNone(reopened["file_delete"]["previous_answer"])
            self.assertFalse(engine.summary()["ready"])

    def test_text_change_is_detected(self):
        with GrillMeEngine(self.root) as engine:
            started = engine.start(
                "Küçük bir metin düzeltmesi yap", origin_platform="codex",
                mode="required")
            scope = started["question"]
            self.assertEqual(scope["question_key"], "scope")
            engine.answer(scope["question_id"], text="Only billing screens")
            back = engine.back(scope["question_id"])
            self.assertEqual(back["question"]["previous_answer"]["text"],
                             "Only billing screens")
            changed = engine.answer(scope["question_id"], text="Only auth screens")
            self.assertTrue(changed["changed"])

    def test_cross_ide_resume_returns_the_exact_open_question(self):
        with GrillMeEngine(self.root) as codex:
            started = self._start(codex, "Oturum yönetimi ekle")
            auth = started["question"]
            first = codex.answer(auth["question_id"], option="jwt")
            codex.answer(first["next"]["question_id"], option="refresh")
            codex.back(auth["question_id"])
            changed = codex.answer(auth["question_id"], option="cookie")
            expected_id = changed["next"]["question_id"]
            expected_key = changed["next"]["question_key"]
        with GrillMeEngine(self.root) as opencode:
            resumed = opencode.resume()
            self.assertEqual(resumed["interview"]["origin_platform"], "codex")
            self.assertEqual(resumed["interview"]["status"], "open")
            self.assertEqual(resumed["question"]["question_id"], expected_id)
            self.assertEqual(resumed["question"]["question_key"], expected_key)
            self.assertEqual(expected_key, "session_expiry")
            self.assertIn(
                "jwt_refresh",
                {item["question_key"]
                 for item in resumed["invalidated_questions"]})
            answered = opencode.answer(resumed["question"]["question_id"],
                                       option="idle30")
            self.assertIsNone(answered["next"])
            self.assertTrue(opencode.approve()["approved"])

    def test_contract_uses_only_confirmed_decisions_after_invalidation(self):
        with GrillMeEngine(self.root) as engine:
            started = self._start(engine, "Oturum yönetimi ekle")
            auth = started["question"]
            first = engine.answer(auth["question_id"], option="jwt")
            engine.answer(first["next"]["question_id"], option="refresh")
            engine.back(auth["question_id"])
            changed = engine.answer(auth["question_id"], option="cookie")
            engine.answer(changed["next"]["question_id"], option="idle30")
            approved = engine.approve()
        self.assertTrue(approved["approved"])
        contract = load_current(self.root)
        topics = {item["topic"]: item["decision"]
                  for item in contract["confirmed_decisions"]}
        self.assertEqual(set(topics), {"auth_session", "session_expiry"})
        self.assertIn("cookie", topics["auth_session"])
        self.assertNotIn("jwt", topics["auth_session"].casefold())


if __name__ == "__main__":
    unittest.main()
