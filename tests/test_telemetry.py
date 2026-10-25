"""Tests for the optional, no-op-safe OpenTelemetry adapter."""
from __future__ import annotations

import math
import unittest
from unittest import mock

from tools import telemetry


class FakeSpan:
    def __init__(self) -> None:
        self.attributes = {}
        self.events = []
        self.statuses = []
        self.ended = False

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value

    def add_event(self, name, attributes=None) -> None:
        self.events.append((name, attributes or {}))

    def set_status(self, status) -> None:
        self.statuses.append(status)

    def end(self) -> None:
        self.ended = True

    def is_recording(self) -> bool:
        return True


class FakeTracer:
    def __init__(self, span=None, fail=False) -> None:
        self.span = span or FakeSpan()
        self.fail = fail
        self.names = []

    def start_span(self, name):
        if self.fail:
            raise RuntimeError("simulated SDK/collector failure")
        self.names.append(name)
        return self.span


class TelemetryTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = (
            telemetry._TRACER,
            telemetry._PROVIDER,
            telemetry._CONFIG,
            telemetry._CONFIGURED,
        )
        telemetry._TRACER = None
        telemetry._PROVIDER = None
        telemetry._CONFIG = None
        telemetry._CONFIGURED = False
        self.addCleanup(self._restore)

    def _restore(self):
        (
            telemetry._TRACER,
            telemetry._PROVIDER,
            telemetry._CONFIG,
            telemetry._CONFIGURED,
        ) = self._saved


class AvailabilityTests(TelemetryTestCase):
    def test_availability_is_a_boolean_and_never_raises(self):
        self.assertIsInstance(telemetry.otel_available(), bool)

    def test_configure_is_noop_when_otel_is_absent(self):
        with mock.patch.object(telemetry, "otel_available", return_value=False) as available:
            with mock.patch.object(telemetry, "_build_tracer") as build:
                self.assertFalse(telemetry.configure())
                self.assertFalse(telemetry.configure(service_name="other"))
                available.assert_called()
                build.assert_not_called()
        self.assertIsNone(telemetry._TRACER)
        self.assertFalse(telemetry._CONFIGURED)

    def test_configure_validates_service_and_endpoint_before_sdk_setup(self):
        with mock.patch.object(telemetry, "otel_available", return_value=True):
            with mock.patch.object(telemetry, "_build_tracer") as build:
                self.assertFalse(telemetry.configure(service_name=""))
                self.assertFalse(telemetry.configure(service_name="x" * 101))
                self.assertFalse(telemetry.configure(service_name=123))
                self.assertFalse(telemetry.configure(endpoint="file:///tmp/traces"))
                self.assertFalse(telemetry.configure(endpoint="ftp://localhost/traces"))
                self.assertFalse(telemetry.configure(endpoint="http://user:pass@localhost/traces"))
                build.assert_not_called()

    def test_configure_is_idempotent_and_refuses_conflicts(self):
        calls = []

        def fake_build(service_name, endpoint):
            calls.append((service_name, endpoint))
            return (object(), None)

        with mock.patch.object(telemetry, "otel_available", return_value=True):
            with mock.patch.object(telemetry, "_build_tracer", side_effect=fake_build):
                self.assertTrue(telemetry.configure())
                self.assertTrue(telemetry.configure())
                self.assertEqual(calls, [(telemetry.SERVICE_NAME, None)])
                self.assertFalse(telemetry.configure(service_name="other"))
                self.assertFalse(telemetry.configure(endpoint="http://127.0.0.1:4318/v1/traces"))
                self.assertTrue(telemetry.configure(service_name=telemetry.SERVICE_NAME))
                self.assertEqual(len(calls), 1)
        self.assertTrue(telemetry._CONFIGURED)
        self.assertEqual(telemetry._CONFIG, (telemetry.SERVICE_NAME, None))

    def test_missing_collector_or_sdk_failure_does_not_break_the_caller(self):
        with mock.patch.object(telemetry, "otel_available", return_value=True):
            with mock.patch.object(
                telemetry, "_build_tracer", side_effect=RuntimeError("collector absent")
            ):
                self.assertFalse(
                    telemetry.configure(endpoint="http://127.0.0.1:4318/v1/traces")
                )
        entered = False
        with telemetry.span("run.start", project_id="p_1", status="ok") as current:
            entered = True
            self.assertIsInstance(current, telemetry.NoopSpan)
        self.assertTrue(entered)
        self.assertFalse(telemetry.record_event("check.passed", status="passed"))

    def test_build_setup_must_return_a_tracer(self):
        with mock.patch.object(telemetry, "otel_available", return_value=True):
            with mock.patch.object(telemetry, "_build_tracer", return_value=(None, None)):
                self.assertFalse(telemetry.configure())
        self.assertFalse(telemetry._CONFIGURED)


class TeamworkMetricTests(TelemetryTestCase):
    def test_teamwork_metrics_are_whitelisted_without_prompt_or_secret_fields(self):
        clean = telemetry.safe_attributes({
            "milestone_id": "M2",
            "pattern": "distributed_coding",
            "workflow": "teamwork",
            "benchmark_mode": "parallel",
            "model": "opencode-go/deepseek-v4.1-flash",
            "provider": "opencode",
            "wall_time_ms": 1200,
            "queue_wait_ms": 30,
            "dispatch_latency_ms": 12,
            "agent_start_latency_ms": 900,
            "test_duration_ms": 5000,
            "review_duration_ms": 700,
            "repair_iterations": 1,
            "retry_count": 0,
            "parallel_overlap_ms": 450,
            "verified_completion": True,
            "verified_completion_rate": 1.0,
            "success": True,
            "failure": False,
            "sse_reconnects": 1,
            "ui_event_lag_ms": 85,
            "usage_input": 1000,
            "usage_output": 200,
            "usage_cost": 0.012,
            "prompt": "secret",
            "raw_output": "secret",
            "api_token": "secret",
        })
        self.assertNotIn("prompt", clean)
        self.assertNotIn("raw_output", clean)
        self.assertNotIn("api_token", clean)
        for key in ("milestone_id", "pattern", "wall_time_ms", "parallel_overlap_ms",
                    "verified_completion_rate", "usage_cost", "model"):
            self.assertIn(key, clean)
        self.assertEqual(clean["wall_time_ms"], 1200)
        self.assertEqual(clean["usage_cost"], 0.012)

class NoopSpanTests(TelemetryTestCase):
    def test_span_without_tracer_runs_body_and_yields_noop(self):
        entered = False
        with telemetry.span("task", project_id="p_1") as current:
            entered = True
            self.assertIsInstance(current, telemetry.NoopSpan)
            current.set_attribute("prompt", "secret")
            current.add_event("x")
            current.end()
        self.assertTrue(entered)
        self.assertFalse(telemetry.record_event("task.event"))

    def test_body_exception_propagates_without_telemetry(self):
        with self.assertRaises(RuntimeError):
            with telemetry.span("task"):
                raise RuntimeError("product failure")

    def test_noop_span_api(self):
        current = telemetry.NoopSpan()
        self.assertFalse(current.is_recording())
        self.assertIsNone(current.set_attribute("status", "ok"))
        self.assertIsNone(current.add_event("event"))
        self.assertIsNone(current.set_status(object()))
        self.assertIsNone(current.end())
        with current as entered:
            self.assertIs(entered, current)
        self.assertFalse(current.__exit__(None, None, None))


class ActiveSpanTests(TelemetryTestCase):
    def test_span_exports_only_whitelisted_attributes(self):
        fake = FakeSpan()
        tracer = FakeTracer(fake)
        telemetry._TRACER = tracer
        telemetry._CONFIGURED = True
        with telemetry.span(
            "run.start",
            project_id="p_1",
            run_id="r_1",
            task_id="t_1",
            attempt_id="a_1",
            agent_id="agent_1",
            platform="opencode",
            status="working",
            prompt="do not export",
            secret_value="do not export",
            token="do not export",
            password="do not export",
            content="do not export",
            source_code="do not export",
            command="do not export",
            output="do not export",
            unknown_field="dropped",
            count=2,
        ) as current:
            self.assertIs(current, fake)
        self.assertEqual(
            fake.attributes,
            {
                "project_id": "p_1",
                "run_id": "r_1",
                "task_id": "t_1",
                "attempt_id": "a_1",
                "agent_id": "agent_1",
                "platform": "opencode",
                "status": "working",
                "count": 2,
            },
        )
        self.assertEqual(tracer.names, ["run.start"])
        self.assertTrue(fake.ended)

    def test_span_with_start_failure_falls_back_to_noop(self):
        telemetry._TRACER = FakeTracer(fail=True)
        telemetry._CONFIGURED = True
        with telemetry.span("task") as current:
            self.assertIsInstance(current, telemetry.NoopSpan)

    def test_body_exception_propagates_and_span_is_ended(self):
        fake = FakeSpan()
        telemetry._TRACER = FakeTracer(fake)
        telemetry._CONFIGURED = True
        with self.assertRaises(ValueError):
            with telemetry.span("task"):
                raise ValueError("boom")
        self.assertTrue(fake.ended)

    def test_span_name_is_sanitized(self):
        fake = FakeSpan()
        tracer = FakeTracer(fake)
        telemetry._TRACER = tracer
        telemetry._CONFIGURED = True
        with telemetry.span("bad\x00name with spaces"):
            pass
        self.assertEqual(tracer.names, ["bad_name_with_spaces"])

    def test_record_event_is_false_without_otel_runtime(self):
        telemetry._TRACER = FakeTracer()
        telemetry._CONFIGURED = True
        self.assertFalse(telemetry.record_event("check.passed", status="passed"))


class AttributeFilteringTests(unittest.TestCase):
    def test_required_sensitive_fragments_are_dropped_even_if_allowlisted(self):
        dangerous = (
            "prompt_text",
            "client_secret",
            "access_token",
            "password_hint",
            "content_type",
            "source_code",
            "command_line",
            "tool_output",
            "api_credential",
            "authorization_header",
        )
        allowlist = telemetry.SAFE_ATTRIBUTE_KEYS | set(dangerous)
        with mock.patch.object(telemetry, "SAFE_ATTRIBUTE_KEYS", allowlist):
            cleaned = telemetry.safe_attributes(
                {"status": "ok", **{key: "sensitive" for key in dangerous}}
            )
        self.assertEqual(cleaned, {"status": "ok"})

    def test_whitelist_normalization_and_scalar_limiting(self):
        cleaned = telemetry.safe_attributes(
            {
                "Status": "ok",
                "run_id": "r_1",
                "unknown_field": "dropped",
                "prompt": "dropped",
                "nested": {"prompt": "dropped"},
                "items": ["dropped"],
                "nan": math.nan,
                "infinity": math.inf,
                "count": 3,
                "retry": True,
                "duration_ms": 2.5,
                "empty": "   ",
                "reason": "a" * 500,
                "event": "a\nb\x00c",
            }
        )
        self.assertEqual(
            set(cleaned),
            {"status", "run_id", "count", "retry", "duration_ms", "reason", "event"},
        )
        self.assertEqual(cleaned["status"], "ok")
        self.assertEqual(len(cleaned["reason"]), telemetry.MAX_ATTRIBUTE_LENGTH)
        self.assertEqual(cleaned["event"], "a b c")
        self.assertIs(cleaned["retry"], True)
        self.assertEqual(cleaned["count"], 3)
        self.assertEqual(cleaned["duration_ms"], 2.5)

    def test_non_mapping_input_is_empty(self):
        self.assertEqual(telemetry.safe_attributes(None), {})
        self.assertEqual(telemetry.safe_attributes([("status", "ok")]), {})


if __name__ == "__main__":
    unittest.main()
