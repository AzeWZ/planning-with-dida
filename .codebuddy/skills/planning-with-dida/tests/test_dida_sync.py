#!/usr/bin/env python3
"""Tests for dida-sync.py using a fake dida CLI.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dida-sync.py"

FAKE = r'''#!/usr/bin/env python3
import json
import os
import sys

STATE = os.environ["FAKE_DIDA_STATE"]


def load():
    with open(STATE, encoding="utf-8") as handle:
        return json.load(handle)


def save(data):
    with open(STATE, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def flag(args, name, default=None):
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return default


def main(argv):
    data = load()
    tasks = data["tasks"]
    command = argv[0] if argv else ""

    if command == "project" and len(argv) > 1 and argv[1] == "list":
        print(json.dumps(data["projects"], ensure_ascii=False))
        return 0

    if command == "project" and len(argv) > 1 and argv[1] == "data":
        project_id = argv[2]
        active = [task for task in tasks.values() if task["projectId"] == project_id and task["status"] != 2]
        print(json.dumps({"project": {"id": project_id, "name": "Project"}, "tasks": active, "columns": []}, ensure_ascii=False))
        return 0

    if command == "task" and len(argv) > 1 and argv[1] == "completed":
        project_id = flag(argv, "--projects")
        completed = [task for task in tasks.values() if task["projectId"] == project_id and task["status"] == 2]
        print(json.dumps(completed, ensure_ascii=False))
        return 0

    if command == "task" and len(argv) > 1 and argv[1] == "search":
        keyword = argv[2]
        project_id = flag(argv, "--projects")
        matches = [task for task in tasks.values() if task["projectId"] == project_id and keyword in task["title"]]
        print(json.dumps(matches, ensure_ascii=False))
        return 0

    if command == "task" and len(argv) > 1 and argv[1] == "get":
        task = tasks.get(argv[3])
        if not task:
            fail("404 not found")
        print(json.dumps(task, ensure_ascii=False))
        return 0

    if command == "task" and len(argv) > 1 and argv[1] == "create":
        new_id = "t" + str(data["next"])
        data["next"] += 1
        tags = flag(argv, "--tags", "")
        tags = [part.strip() for part in tags.split(",") if part.strip()]
        task = {
            "id": new_id,
            "projectId": flag(argv, "--project"),
            "parentId": flag(argv, "--parent-id") or None,
            "title": flag(argv, "--title", ""),
            "content": flag(argv, "--content", ""),
            "status": 0,
            "tags": tags,
            "sortOrder": int(flag(argv, "--sort-order", "0") or 0),
            "childIds": [],
        }
        tasks[new_id] = task
        parent_id = task["parentId"]
        if parent_id and parent_id in tasks:
            tasks[parent_id].setdefault("childIds", []).append(new_id)
        save(data)
        print(json.dumps(task, ensure_ascii=False))
        return 0

    if command == "task" and len(argv) > 1 and argv[1] == "update":
        task = tasks.get(argv[2])
        if not task:
            fail("404 not found")
        if flag(argv, "--title") is not None:
            task["title"] = flag(argv, "--title")
        if flag(argv, "--content") is not None:
            task["content"] = flag(argv, "--content")
        if flag(argv, "--status") is not None:
            task["status"] = int(flag(argv, "--status"))
        if flag(argv, "--tags") is not None:
            raw = flag(argv, "--tags")
            task["tags"] = [part.strip() for part in raw.split(",") if part.strip()]
        save(data)
        print(json.dumps(task, ensure_ascii=False))
        return 0

    if command == "task" and len(argv) > 1 and argv[1] == "complete":
        task = tasks.get(argv[3])
        if not task:
            fail("404 not found")
        task["status"] = 2
        save(data)
        # The real CLI prints plain text here, not JSON.
        print("任务已完成")
        return 0

    if command == "task" and len(argv) > 1 and argv[1] == "delete":
        tasks.pop(argv[3], None)
        save(data)
        return 0

    fail("unsupported fake command: " + " ".join(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


PLAN = """# Task Plan: Demo Plan

## Goal
Ship the demo.

## Next Step
Start phase 1.

## Current Phase
Phase 1

## Phases

### Phase 1: Requirements
- [ ] Understand the ask
- **Status:** in_progress

### Phase 2: Build
- [ ] Write the code
- **Status:** pending

### Phase 3: Verify
- [ ] Run the tests
- **Status:** pending

## Decisions Made
| Decision | Rationale |
|----------|-----------|

## Errors Encountered
| Error | Resolution |
|-------|------------|
"""


class DidaSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plan = self.root / "task_plan.md"
        self.plan.write_text(PLAN, encoding="utf-8")
        self.state = self.root / "state.json"
        self.state.write_text(
            json.dumps(
                {
                    "projects": [{"id": "p1", "name": "Project"}],
                    "next": 100,
                    "tasks": {},
                }
            ),
            encoding="utf-8",
        )
        self.fake = self.root / "dida"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.fake.chmod(0o755)
        self.env = dict(os.environ)
        self.env["DIDA_BIN"] = str(self.fake)
        self.env["FAKE_DIDA_STATE"] = str(self.state)
        self.env.pop("PLAN_ID", None)
        self.env.pop("DIDA_PROJECT_ID", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_sync(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--plan", str(self.plan), *args],
            cwd=str(self.root),
            env=env or self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def tasks(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["tasks"]

    def sidecar(self):
        return json.loads((self.root / ".dida.json").read_text(encoding="utf-8"))

    def test_init_creates_parent_and_phase_tasks(self):
        result = self.run_sync("init", "--name", "Demo")
        self.assertEqual(result.returncode, 0, result.stderr)

        tasks = self.tasks()
        self.assertEqual(len(tasks), 4)
        parent = next(task for task in tasks.values() if task["parentId"] is None)
        self.assertEqual(parent["title"], "计划：Demo")
        self.assertEqual(len(parent["childIds"]), 3)
        children = sorted(
            (task for task in tasks.values() if task["parentId"] == parent["id"]),
            key=lambda task: task["title"],
        )
        self.assertEqual(
            [task["title"] for task in children],
            ["P1 Requirements", "P2 Build", "P3 Verify"],
        )
        self.assertEqual(children[0]["tags"], ["进行中"])

        sidecar = self.sidecar()
        self.assertEqual(sidecar["projectId"], "p1")
        self.assertEqual(sidecar["parentId"], parent["id"])
        self.assertEqual(set(sidecar["phases"]), {"1", "2", "3"})

    def test_init_is_idempotent(self):
        self.assertEqual(self.run_sync("init", "--name", "Demo").returncode, 0)
        self.assertEqual(self.run_sync("init", "--name", "Demo").returncode, 0)
        self.assertEqual(len(self.tasks()), 4)

    def test_pull_updates_statuses_and_current_phase(self):
        self.assertEqual(self.run_sync("init", "--name", "Demo").returncode, 0)
        sidecar = self.sidecar()
        tasks = self.tasks()
        tasks[sidecar["phases"]["1"]]["status"] = 2
        tasks[sidecar["phases"]["2"]]["tags"] = ["进行中"]
        self.state.write_text(
            json.dumps(
                {
                    "projects": [{"id": "p1", "name": "Project"}],
                    "next": 100,
                    "tasks": tasks,
                }
            ),
            encoding="utf-8",
        )

        result = self.run_sync("pull", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.plan.read_text(encoding="utf-8")
        self.assertIn("### Phase 1: Requirements\n- [ ] Understand the ask\n- **Status:** complete", text)
        self.assertIn("### Phase 2: Build\n- [ ] Write the code\n- **Status:** in_progress", text)
        self.assertIn("## Current Phase\nPhase 2", text)

    def test_phase_updates_remote_and_local(self):
        self.assertEqual(self.run_sync("init", "--name", "Demo").returncode, 0)
        sidecar = self.sidecar()
        result = self.run_sync("phase", "2", "complete")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tasks()[sidecar["phases"]["2"]]["status"], 2)
        text = self.plan.read_text(encoding="utf-8")
        self.assertIn("### Phase 2: Build\n- [ ] Write the code\n- **Status:** complete", text)

    def test_complete_finishes_all_phases_and_parent(self):
        self.assertEqual(self.run_sync("init", "--name", "Demo").returncode, 0)
        sidecar = self.sidecar()
        result = self.run_sync("complete")
        self.assertEqual(result.returncode, 0, result.stderr)
        tasks = self.tasks()
        self.assertTrue(all(tasks[task_id]["status"] == 2 for task_id in sidecar["phases"].values()))
        self.assertEqual(tasks[sidecar["parentId"]]["status"], 2)
        self.assertIn("## Current Phase\nAll phases complete", self.plan.read_text(encoding="utf-8"))

    def test_next_updates_plan_and_parent_content(self):
        self.assertEqual(self.run_sync("init", "--name", "Demo").returncode, 0)
        sidecar = self.sidecar()
        result = self.run_sync("next", "Review the PR")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.plan.read_text(encoding="utf-8")
        self.assertIn("## Next Step\nReview the PR", text)
        parent = self.tasks()[sidecar["parentId"]]
        self.assertIn("下一步：Review the PR", parent["content"])

    def test_offline_pull_is_quiet_and_keeps_cache(self):
        self.assertEqual(self.run_sync("init", "--name", "Demo").returncode, 0)
        before = self.plan.read_text(encoding="utf-8")
        env = dict(self.env)
        env["DIDA_BIN"] = "/nonexistent/dida"
        result = self.run_sync("pull", "--quiet", "--force", env=env)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.plan.read_text(encoding="utf-8"), before)

    def test_pull_without_sidecar_is_noop(self):
        result = self.run_sync("pull", "--quiet", "--force")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.tasks(), {})
        self.assertFalse((self.root / ".dida.json").exists())

    def test_hash_in_goal_is_not_a_tag(self):
        self.plan.write_text(
            PLAN.replace("Ship the demo.", "Ship release #12."),
            encoding="utf-8",
        )
        self.assertEqual(self.run_sync("init", "--name", "Demo").returncode, 0)
        parent = next(task for task in self.tasks().values() if task["parentId"] is None)
        self.assertIn("＃12", parent["content"])
        self.assertNotIn("#12", parent["content"])

    def test_restore_rebuilds_plan_from_dida(self):
        self.assertEqual(self.run_sync("init", "--name", "Demo").returncode, 0)
        sidecar = self.sidecar()
        self.plan.unlink()
        result = self.run_sync("restore", "--parent", sidecar["parentId"])
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.plan.read_text(encoding="utf-8")
        self.assertIn("### Phase 1: Requirements", text)
        self.assertIn("**Status:** in_progress", text)
        self.assertIn("## Current Phase\nPhase 1", text)


if __name__ == "__main__":
    unittest.main()
