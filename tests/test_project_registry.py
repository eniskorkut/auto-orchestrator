from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import project_registry as registry_module
from tools.project_registry import (
    RegistryError,
    ProjectRegistry,
    bindings_path,
    canonical_root,
    default_registry_path,
    load_bindings,
    project_id_for,
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.registry_path = self.base / "config" / "projects.json"
        self.registry = ProjectRegistry(self.registry_path)

    def make_dir(self, name: str) -> Path:
        path = self.base / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def add_project(self, name="alpha", **kwargs):
        return self.registry.add(self.make_dir(name), name=name.title(), **kwargs)


class RegistryPathTests(unittest.TestCase):
    def test_environment_and_home_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(os.environ, {"AUTO_ORCHESTRATOR_HOME": temp}):
                self.assertEqual(default_registry_path(),
                                 Path(temp) / "projects.json")
        expected_home = Path(temp) / "home"
        with mock.patch.dict(os.environ, {"AUTO_ORCHESTRATOR_HOME": ""}), \
                mock.patch.object(Path, "home", return_value=expected_home):
            self.assertEqual(default_registry_path(),
                             expected_home / ".auto-orchestrator" / "projects.json")

    def test_canonical_root_rejects_missing_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with self.assertRaisesRegex(RegistryError, "does not exist"):
                canonical_root(base / "missing")
            regular = base / "file.txt"
            regular.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(RegistryError, "directory"):
                canonical_root(regular)
            target = base / "target"
            target.mkdir()
            link = base / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(RegistryError, "symbolic link"):
                canonical_root(link)


class RegistryProjectTests(RegistryTestCase):
    def test_add_is_idempotent_and_ids_are_deterministic(self):
        project, created = self.add_project()
        self.assertTrue(created)
        self.assertRegex(project["project_id"], r"^p_[0-9a-f]{16}$")
        self.assertEqual(project["project_id"],
                         project_id_for((self.base / "alpha").resolve()))
        again, created_again = self.registry.add(self.base / "alpha" / ".")
        self.assertFalse(created_again)
        self.assertEqual(again["project_id"], project["project_id"])
        self.assertEqual(len(self.registry.projects()), 1)
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 2)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.registry_path.stat().st_mode), 0o600)

    def test_duplicate_canonical_root_fails_closed(self):
        project, _ = self.add_project()
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        duplicate = json.loads(json.dumps(payload["projects"][0]))
        duplicate["project_id"] = "p_" + "0" * 16
        payload["projects"].append(duplicate)
        self.registry_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(RegistryError, "Duplicate canonical"):
            self.registry.load()

    def test_portable_manifest_keeps_project_id_after_relocation(self):
        from tools.project_identity import ensure_project_identity
        source = self.make_dir("portable")
        original = ensure_project_identity(source)
        project, created = self.registry.add(source, name="Portable")
        self.assertTrue(created)
        self.assertEqual(project["project_id"], original["project_id"])
        moved = self.base / "portable-moved"
        import shutil
        shutil.copytree(source, moved)
        relocated, created_again = self.registry.add(moved, name="Portable")
        self.assertFalse(created_again)
        self.assertEqual(relocated["project_id"], original["project_id"])
        self.assertEqual(relocated["root"], str(canonical_root(moved)))
        self.assertEqual(len(self.registry.projects()), 1)

    def test_invalid_portable_manifest_is_not_trusted(self):
        root = self.make_dir("broken")
        path = root / ".auto-orchestrator" / "project.json"
        path.parent.mkdir()
        path.write_text(json.dumps({
            "version": 1, "project_id": "p_" + "b" * 16,
            "project_uuid": "00000000-0000-4000-8000-000000000000",
        }))
        project, created = self.registry.add(root, name="Broken")
        self.assertTrue(created)
        self.assertNotEqual(project["project_id"], "p_" + "b" * 16)
        self.assertEqual(project["project_id"], project_id_for(canonical_root(root)))

    def test_project_id_collision_between_distinct_roots_fails(self):
        project, _ = self.add_project()
        second = self.make_dir("beta")
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        collision = json.loads(json.dumps(payload["projects"][0]))
        collision["root"] = str(second.resolve())
        # The deterministic id does not match this root, so validation fails
        # closed instead of silently sharing an identity.
        payload["projects"].append(collision)
        self.registry_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(RegistryError):
            self.registry.load()

    def test_symlinked_registry_is_rejected(self):
        target = self.base / "real-registry.json"
        target.write_text('{"version": 2, "projects": []}', encoding="utf-8")
        self.registry_path.parent.mkdir(parents=True)
        try:
            self.registry_path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        with self.assertRaisesRegex(RegistryError, "symbolic link"):
            self.registry.load()

    def test_traversal_and_invalid_identifiers_are_rejected(self):
        project, _ = self.add_project()
        for value in ("../etc", "p_../../x", "", "p_" + "g" * 16):
            with self.assertRaises(RegistryError):
                self.registry.get(value)
        with self.assertRaises(RegistryError):
            self.registry.remove("../" + project["project_id"])
        with self.assertRaises(RegistryError):
            self.registry.add_agent("../x", name="A", role="R")
        agent = self.registry.add_agent(project["project_id"], name="A", role="R")
        with self.assertRaises(RegistryError):
            self.registry.bind_agent(project["project_id"], agent["agent_definition_id"],
                                     digest("native"), run_id="../../secret")

    def test_remove_only_deletes_metadata(self):
        project, _ = self.add_project()
        evidence = self.base / "alpha" / ".auto-orchestrator" / "runs" / "run-1.jsonl"
        evidence.parent.mkdir(parents=True)
        evidence.write_text('{"event":"start"}\n', encoding="utf-8")
        worktree = self.base / "alpha" / "worktree"
        worktree.mkdir()
        removed = self.registry.remove(project["project_id"])
        self.assertEqual(removed["project_id"], project["project_id"])
        self.assertTrue(evidence.is_file())
        self.assertTrue(worktree.is_dir())
        self.assertEqual(self.registry.projects(), [])

    def test_atomic_failure_preserves_the_previous_registry(self):
        self.add_project("alpha")
        before = self.registry_path.read_bytes()
        with mock.patch.object(registry_module.os, "replace",
                               side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RegistryError, "write failed"):
                self.registry.add(self.make_dir("beta"))
        self.assertEqual(self.registry_path.read_bytes(), before)
        leftovers = [path.name for path in self.registry_path.parent.iterdir()
                     if path.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_update_and_validation(self):
        project, _ = self.add_project(theme="ai")
        updated = self.registry.update(project["project_id"], name="Renamed",
                                       description="desc", theme="web")
        self.assertEqual(updated["name"], "Renamed")
        self.assertEqual(updated["theme"], "web")
        with self.assertRaises(RegistryError):
            self.registry.update(project["project_id"], theme="unknown")
        with self.assertRaises(RegistryError):
            self.registry.add_agent(project["project_id"], name="A", role="R",
                                    platform="not-a-platform")
        with self.assertRaises(RegistryError):
            self.registry.add_agent(project["project_id"], name="A", role="R",
                                    color="red")
        agent = self.registry.add_agent(project["project_id"], name="Ada", role="Engineer")
        self.assertRegex(agent["agent_definition_id"], r"^a_[0-9a-f]{12}$")
        self.assertEqual(self.registry.get_agent(project["project_id"],
                                                 agent["agent_definition_id"])["name"], "Ada")


class RegistryBindingTests(RegistryTestCase):
    def setUp(self):
        super().setUp()
        self.project, _ = self.add_project()
        self.agent = self.registry.add_agent(self.project["project_id"],
                                             name="Ada", role="Engineer",
                                             platform="opencode")
        self.project_id = self.project["project_id"]
        self.agent_id = self.agent["agent_definition_id"]

    def test_binding_is_explicit_idempotent_and_stores_no_raw_native_id(self):
        native_hash = digest("opencode|tool-call-123")
        binding, created = self.registry.bind_agent(
            self.project_id, self.agent_id, native_hash,
            run_id="run-1", session_id="session-1", task_id="task-1")
        self.assertTrue(created)
        self.assertEqual(binding["run_id"], "run-1")
        path = bindings_path(self.base / "alpha")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(set(payload["bindings"][0]),
                         {"agent_definition_id", "native_hash", "run_id",
                          "session_id", "task_id", "bound_at"})
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("tool-call-123", raw)
        again, created_again = self.registry.bind_agent(
            self.project_id, self.agent_id, native_hash,
            run_id="run-1", session_id="session-1", task_id="task-1")
        self.assertFalse(created_again)
        self.assertEqual(again["native_hash"], native_hash)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_rebinding_replaces_the_previous_explicit_binding(self):
        native_hash = digest("native")
        self.registry.bind_agent(self.project_id, self.agent_id, native_hash,
                                 run_id="run-1")
        replacement, created = self.registry.bind_agent(
            self.project_id, self.agent_id, digest("native-2"), run_id="run-2")
        self.assertTrue(created)
        bindings = load_bindings(self.base / "alpha")["bindings"]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["run_id"], "run-2")
        self.assertEqual(replacement["run_id"], "run-2")

    def test_invalid_hashes_and_validation(self):
        for value in ("", "abc", digest("x").upper(), "f" * 63, "z" * 64):
            with self.assertRaises(RegistryError):
                self.registry.bind_agent(self.project_id, self.agent_id, value,
                                         run_id="run-1")
        with self.assertRaises(RegistryError):
            self.registry.bind_agent(self.project_id, "a_" + "0" * 12,
                                     digest("x"), run_id="run-1")
        with self.assertRaises(RegistryError):
            self.registry.bind_agent(self.project_id, self.agent_id, digest("x"),
                                     run_id="run-1", task_id="task/../escape")

    def test_duplicate_native_hash_between_agents_is_rejected(self):
        other = self.registry.add_agent(self.project_id, name="Grace", role="Reviewer")
        native_hash = digest("shared")
        self.registry.bind_agent(self.project_id, self.agent_id, native_hash,
                                 run_id="run-1")
        with self.assertRaisesRegex(RegistryError, "already bound"):
            self.registry.bind_agent(self.project_id, other["agent_definition_id"],
                                     native_hash, run_id="run-2")

    def test_unknown_or_raw_native_fields_fail_closed(self):
        path = bindings_path(self.base / "alpha")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "bindings": [{
                "agent_definition_id": self.agent_id,
                "native_hash": digest("x"),
                "run_id": "run-1",
                "native_id": "raw-secret",
                "bound_at": 0,
            }],
        }), encoding="utf-8")
        with self.assertRaisesRegex(RegistryError, "unsupported fields"):
            load_bindings(self.base / "alpha")

    def test_symlinked_bindings_are_rejected(self):
        project_root = self.base / "alpha"
        directory = project_root / ".auto-orchestrator"
        directory.mkdir(parents=True, exist_ok=True)
        outside = self.base / "outside.json"
        outside.write_text('{"version":1,"bindings":[]}', encoding="utf-8")
        path = bindings_path(project_root)
        try:
            path.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        with self.assertRaisesRegex(RegistryError, "symbolic link"):
            load_bindings(project_root)
        with self.assertRaisesRegex(RegistryError, "symbolic link"):
            self.registry.bind_agent(self.project_id, self.agent_id, digest("x"),
                                     run_id="run-1")

    def test_bindings_directory_symlink_is_rejected(self):
        project_root = self.base / "alpha"
        outside = self.base / "outside-directory"
        outside.mkdir()
        try:
            (project_root / ".auto-orchestrator").symlink_to(outside,
                                                             target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        with self.assertRaisesRegex(RegistryError, "symbolic link"):
            self.registry.bind_agent(self.project_id, self.agent_id, digest("x"),
                                     run_id="run-1")


class RegistryMigrationAndDiscoveryTests(RegistryTestCase):
    def test_legacy_version_migrates_and_unknown_version_fails(self):
        legacy_root = self.make_dir("legacy")
        payload = {
            "version": 1,
            "projects": [{
                "root": str(legacy_root),
                "name": "Legacy",
                "agents": [{"name": "Old Agent", "role": "Engineer"}],
            }],
        }
        self.registry_path.parent.mkdir(parents=True)
        self.registry_path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = self.registry.load()
        self.assertEqual(loaded["version"], 2)
        project = loaded["projects"][0]
        self.assertEqual(project["project_id"], project_id_for(legacy_root.resolve()))
        self.assertRegex(project["agents"][0]["agent_definition_id"],
                         r"^a_[0-9a-f]{12}$")
        result = self.registry.migrate()
        self.assertEqual(result["version"], 2)
        self.assertEqual(json.loads(self.registry_path.read_text(encoding="utf-8"))["version"], 2)

        self.registry_path.write_text(json.dumps({"version": 99, "projects": []}),
                                      encoding="utf-8")
        with self.assertRaisesRegex(RegistryError, "Unsupported registry version"):
            self.registry.load()

    def test_discover_registers_only_evidence_roots(self):
        one = self.make_dir("one")
        two = self.make_dir("two")
        self.make_dir("plain")
        (one / ".auto-orchestrator").mkdir()
        (two / ".auto-orchestrator").mkdir()
        created = self.registry.discover(self.base)
        self.assertEqual(len(created), 2)
        self.assertEqual({project["root"] for project in created},
                         {str(one.resolve()), str(two.resolve())})
        self.assertEqual(self.registry.discover(self.base), [])


if __name__ == "__main__":
    unittest.main()

class RegistryLimitTests(RegistryTestCase):
    def test_oversized_registry_is_reported_as_too_large(self):
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_bytes(b"x" * (registry_module.MAX_REGISTRY_BYTES + 1))
        with self.assertRaisesRegex(RegistryError, "too large"):
            ProjectRegistry(self.registry_path).load()

    def test_save_rejects_too_many_projects_before_writing(self):
        first, _ = self.registry.add(self.make_dir("alpha"), name="Alpha")
        second, _ = self.registry.add(self.make_dir("beta"), name="Beta")
        payload = {"version": 2, "projects": [first, second]}
        with mock.patch.object(registry_module, "MAX_PROJECTS", 1):
            with self.assertRaisesRegex(RegistryError, "bounded array"):
                self.registry.save(payload)
        self.assertEqual(len(self.registry.projects()), 2)

    def test_save_rejects_oversized_encoded_payload(self):
        first, _ = self.registry.add(self.make_dir("alpha"), name="Alpha")
        with mock.patch.object(registry_module, "MAX_REGISTRY_BYTES", 10):
            with self.assertRaisesRegex(RegistryError, "too large"):
                self.registry.save({"version": 2, "projects": [first]})
        self.assertEqual(len(self.registry.projects()), 1)

class BindingLifecycleTests(RegistryTestCase):
    def test_unbind_works_after_project_remove_and_readd(self):
        project, _ = self.registry.add(self.make_dir("alpha"), name="Alpha")
        agent = self.registry.add_agent(project["project_id"], name="Ada", role="Engineer")
        self.registry.bind_agent(project["project_id"], agent["agent_definition_id"],
                                 digest("opencode|call-1"), run_id="run-1")
        self.registry.remove(project["project_id"])
        again, _ = self.registry.add(self.make_dir("alpha"), name="Alpha")
        self.assertEqual(again["project_id"], project["project_id"])
        self.assertEqual(again["agents"], [])
        self.assertTrue(self.registry.unbind_agent(project["project_id"],
                                                   agent["agent_definition_id"]))
        self.assertEqual(load_bindings(self.make_dir("alpha"))["bindings"], [])

class RegistryConcurrencyTests(RegistryTestCase):
    def test_two_instances_do_not_lose_a_project(self):
        import threading
        barrier = threading.Barrier(2)
        errors = []

        def worker(reg, path):
            try:
                barrier.wait(5)
                reg.add(path, name=path.name)
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        alpha = self.make_dir("alpha")
        beta = self.make_dir("beta")
        threads = [
            threading.Thread(target=worker, args=(ProjectRegistry(self.registry_path), alpha)),
            threading.Thread(target=worker, args=(ProjectRegistry(self.registry_path), beta)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual({project["name"] for project in ProjectRegistry(self.registry_path).projects()},
                         {"alpha", "beta"})

class RegistryRobustnessTests(RegistryTestCase):
    def test_deeply_nested_registry_fails_closed(self):
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text('{"version":2,"projects":' + '[' * 100000 + ']' * 100000 + '}', encoding='utf-8')
        with self.assertRaises(RegistryError):
            ProjectRegistry(self.registry_path).load()

    def test_non_finite_timestamp_is_not_written(self):
        project, _ = self.registry.add(self.make_dir("alpha"), name="Alpha")
        updated = self.registry.update(project["project_id"], name="Renamed", now=float("inf"))
        self.assertEqual(updated["updated_at"], 0)
        json.loads(self.registry_path.read_text(encoding="utf-8"))

    def test_write_bindings_does_not_recreate_missing_root(self):
        root = self.make_dir("gone")
        root.rmdir()
        with self.assertRaises(RegistryError):
            registry_module.write_bindings(root, {"version": 1, "bindings": []})
        self.assertFalse(root.exists())
