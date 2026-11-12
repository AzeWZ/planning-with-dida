#!/usr/bin/env python3
"""Dida365 task sync for planning-with-dida.

Dida is the source of truth for plan phases. task_plan.md is a local
projection/cache so the lifecycle hooks can inject context without a network
call on every event.

The script talks to Dida only through the dida CLI (dida on PATH, or
DIDA_BIN). It never stores or prints the API token.

Commands:
  init      Create/update the parent task and one subtask per phase.
  sync      Alias for init (push local titles/goal/next step to Dida).
  pull      Pull phase statuses/titles from Dida into task_plan.md.
  phase     Set one phase status in Dida and in the local cache.
  next      Set the next-step line locally and in the parent task.
  complete  Complete all phases and the parent task.
  status    Show the mapping and the current Dida state.
  restore   Rebuild task_plan.md from Dida when the local cache is lost.

Exit codes:
  0  success (including a quiet pull that could not reach Dida)
  1  usage/config error, or a non-quiet pull that could not reach Dida
  2  the plan is not valid for syncing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

PLAN_FILENAME = "task_plan.md"
SIDECAR_NAME = ".dida.json"
IN_PROGRESS_TAG = "进行中"
PLAN_PREFIX = "计划："
DEFAULT_PROJECT = "Project"
MAX_TITLE = 40

PHASE_RE = re.compile(r"^### Phase (\d+)\s*:\s*(.*?)\s*$")
STATUS_RE = re.compile(
    r"^(\s*(?:-\s*)?)\*\*Status:\*\*\s*(pending|in_progress|complete)\s*$"
)
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
PHASE_PREFIX_RE = re.compile(r"^P(\d+)(?:\s+|$)")
DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")


class DidaError(RuntimeError):
    pass


class PlanError(RuntimeError):
    pass


@dataclass
class Phase:
    number: int
    title: str
    status: str
    heading_index: int
    status_index: int


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_iso(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def one_line(value: str, limit: int = 0) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if limit and len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def sanitize_hash(value: str) -> str:
    # Dida parses "#12" as a tag; write fullwidth so task text stays text.
    return re.sub(r"#(?=\d)", "＃", value or "")


def clean_text(value: str, limit: int = 0) -> str:
    return sanitize_hash(one_line(value, limit))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text_atomic(path: Path, text: str) -> bool:
    old = read_text(path)
    if old == text:
        return False
    if path.is_symlink():
        raise PlanError(f"拒绝写入符号链接: {path}")
    mode = path.stat().st_mode
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return True


def resolve_plan(explicit: Optional[str]) -> Optional[Path]:
    cwd = Path.cwd()
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        candidate = candidate.resolve()
        if not candidate.is_file() or candidate.is_symlink():
            raise PlanError(f"计划文件不存在或不是普通文件: {candidate}")
        return candidate

    plan_id = os.environ.get("PLAN_ID", "").strip()
    if plan_id and re.fullmatch(r"[A-Za-z0-9._-]+", plan_id):
        candidate = cwd / ".planning" / plan_id / PLAN_FILENAME
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve()

    planning = cwd / ".planning"
    if planning.is_dir() and not planning.is_symlink():
        pointer = planning / ".active_plan"
        if pointer.is_file() and not pointer.is_symlink():
            name = pointer.read_text(encoding="utf-8", errors="replace").strip()
            if re.fullmatch(r"[A-Za-z0-9._-]+", name):
                candidate = planning / name / PLAN_FILENAME
                if candidate.is_file() and not candidate.is_symlink():
                    return candidate.resolve()
        newest = None
        newest_mtime = -1.0
        for entry in planning.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            candidate = entry / PLAN_FILENAME
            if candidate.is_file() and not candidate.is_symlink():
                mtime = candidate.stat().st_mtime
                if mtime > newest_mtime:
                    newest = candidate
                    newest_mtime = mtime
        if newest is not None:
            return newest.resolve()

    candidate = cwd / PLAN_FILENAME
    if candidate.is_file() and not candidate.is_symlink():
        return candidate.resolve()
    return None


def sidecar_path(plan_path: Path) -> Path:
    return plan_path.parent / SIDECAR_NAME


def load_sidecar(plan_path: Path) -> dict:
    path = sidecar_path(plan_path)
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_sidecar(plan_path: Path, data: dict) -> None:
    path = sidecar_path(plan_path)
    if path.is_symlink():
        raise PlanError(f"拒绝写入符号链接: {path}")
    fd, tmp_name = tempfile.mkstemp(prefix=".dida.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def section_bounds(lines: list[str], name: str) -> Optional[Tuple[int, int]]:
    start = None
    for index, line in enumerate(lines):
        match = SECTION_RE.match(line)
        if match and match.group(1).strip().lower() == name.lower():
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if SECTION_RE.match(lines[index]):
            end = index
            break
    return start, end


def read_section(lines: list[str], name: str) -> str:
    bounds = section_bounds(lines, name)
    if bounds is None:
        return ""
    start, end = bounds
    body = [line.strip() for line in lines[start + 1 : end]]
    return one_line(" ".join(part for part in body if part))


def replace_section(lines: list[str], name: str, value: str) -> bool:
    bounds = section_bounds(lines, name)
    if bounds is None:
        return False
    start, end = bounds
    body = [value + "\n"]
    if end < len(lines):
        body.append("\n")
    lines[start + 1 : end] = body
    return True


def parse_plan(text: str) -> tuple[list[str], list[Phase], str, str]:
    lines = text.splitlines(keepends=True)
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = PHASE_RE.match(line.rstrip("\n"))
        if match:
            headings.append((index, int(match.group(1)), match.group(2)))

    phases: list[Phase] = []
    for position, (heading_index, number, title) in enumerate(headings):
        end = len(lines)
        if position + 1 < len(headings):
            end = headings[position + 1][0]
        else:
            for index in range(heading_index + 1, len(lines)):
                if SECTION_RE.match(lines[index]) or re.match(r"^###\s+", lines[index]):
                    end = index
                    break
        status_index = -1
        status = ""
        for index in range(heading_index + 1, end):
            match = STATUS_RE.match(lines[index].rstrip("\n"))
            if match:
                status_index = index
                status = match.group(2)
                break
        if status_index < 0:
            raise PlanError(
                f"Phase {number} 缺少 **Status:** 行；先按模板补齐再同步滴答"
            )
        phases.append(Phase(number, title, status, heading_index, status_index))

    if not phases:
        raise PlanError("plan 中没有可同步的 '### Phase N:' 阶段")
    numbers = [phase.number for phase in phases]
    if len(numbers) != len(set(numbers)):
        raise PlanError("plan 中阶段编号重复")
    return lines, phases, read_section(lines, "Goal"), read_section(lines, "Next Step")


def render_plan(
    lines: list[str],
    phases: list[Phase],
    remote_titles: dict[int, str],
    remote_status: dict[int, str],
) -> str:
    output = list(lines)
    for phase in phases:
        title = remote_titles.get(phase.number)
        if title is not None:
            output[phase.heading_index] = f"### Phase {phase.number}: {title}\n"
        status = remote_status.get(phase.number)
        if status is not None:
            line = output[phase.status_index]
            output[phase.status_index] = re.sub(
                r"(\*\*Status:\*\*\s*)(pending|in_progress|complete)",
                lambda match: match.group(1) + status,
                line,
            )

    effective = {phase.number: phase.status for phase in phases}
    effective.update(remote_status)
    ordered = sorted(phases, key=lambda item: item.number)
    in_progress = [phase for phase in ordered if effective[phase.number] == "in_progress"]
    pending = [phase for phase in ordered if effective[phase.number] == "pending"]
    if in_progress:
        current = f"Phase {in_progress[0].number}"
    elif pending:
        current = f"Phase {pending[0].number}"
    else:
        current = "All phases complete"
    replace_section(output, "Current Phase", current)
    return "".join(output)


def derive_name(plan_path: Path, lines: list[str]) -> str:
    first = lines[0].strip() if lines else ""
    match = re.match(r"^#\s+Task Plan:\s*(.+)$", first)
    if match:
        name = match.group(1).strip()
        if name and not name.startswith("[") and name.lower() != "brief description":
            return one_line(name, 34)
    name = plan_path.parent.name
    if name == ".planning":
        name = "task plan"
    else:
        name = DATE_PREFIX_RE.sub("", name).replace("-", " ").strip() or "task plan"
    return one_line(name, 34)


def parent_title(name: str) -> str:
    name = sanitize_hash(one_line(name, 34))
    if name.startswith(PLAN_PREFIX):
        return sanitize_hash(one_line(name, MAX_TITLE))
    return sanitize_hash(one_line(PLAN_PREFIX + name, MAX_TITLE))


def phase_title_remote(number: int, title: str) -> str:
    return sanitize_hash(one_line(f"P{number} {one_line(title)}", MAX_TITLE))


def plan_relative(plan_path: Path) -> str:
    try:
        return str(plan_path.relative_to(Path.cwd()))
    except ValueError:
        return str(plan_path)


def parent_content(
    plan_path: Path,
    goal: str,
    next_step: str,
    phases: list[Phase],
    statuses: Optional[dict[int, str]] = None,
) -> str:
    statuses = statuses or {phase.number: phase.status for phase in phases}
    done = sum(1 for phase in phases if statuses.get(phase.number) == "complete")
    lines = [
        f"{PLAN_PREFIX}{sanitize_hash(plan_relative(plan_path))}",
        f"目标：{clean_text(goal, 500) or '（未填写）'}",
        f"下一步：{clean_text(next_step, 300) or '（未填写）'}",
        f"阶段：{done}/{len(phases)}",
    ]
    return "\n".join(lines)


def child_content(plan_path: Path, number: int, total: int) -> str:
    return "\n".join(
        [
            f"{PLAN_PREFIX}{sanitize_hash(plan_relative(plan_path))}",
            f"阶段：{number}/{total}",
        ]
    )


def dida_bin() -> str:
    value = os.environ.get("DIDA_BIN", "dida")
    if os.sep in value or (os.altsep and os.altsep in value):
        return value
    found = shutil.which(value)
    if not found:
        raise DidaError("找不到 dida CLI；先安装 @suibiji/dida-cli 并执行 dida auth login")
    return found


def run_dida(args: list[str], timeout: int = 15, json_expected: bool = True):
    command = [dida_bin(), *args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DidaError(f"dida 调用失败: {exc}") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise DidaError(message[:800] or f"dida 退出码 {completed.returncode}")
    stdout = completed.stdout.strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        if not json_expected:
            return stdout
        raise DidaError(f"dida 返回不是 JSON: {stdout[:300]}") from exc


def is_not_found(error: DidaError) -> bool:
    text = str(error).lower()
    return any(token in text for token in ("404", "not found", "不存在", "未找到"))


def get_task(project_id: str, task_id: str) -> Optional[dict]:
    try:
        result = run_dida(["task", "get", project_id, task_id, "--json"])
    except DidaError as exc:
        if is_not_found(exc):
            return None
        raise
    return result if isinstance(result, dict) else None


def project_id_for(explicit: Optional[str], sidecar: dict) -> str:
    if explicit:
        return explicit
    if sidecar.get("projectId"):
        return str(sidecar["projectId"])
    env_value = os.environ.get("DIDA_PROJECT_ID", "").strip()
    if env_value:
        return env_value
    projects = run_dida(["project", "list", "--json"])
    if not isinstance(projects, list):
        raise DidaError("dida project list 返回格式异常")
    for project in projects:
        if str(project.get("name", "")).lower() == DEFAULT_PROJECT.lower():
            return str(project["id"])
    raise DidaError("找不到名为 Project 的清单；先创建或传 --project <id>")


def create_task(
    project_id: str,
    title: str,
    content: str,
    parent_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
    sort_order: Optional[int] = None,
) -> dict:
    args = [
        "task",
        "create",
        "--title",
        title,
        "--project",
        project_id,
        "--content",
        content,
        "--json",
    ]
    if parent_id:
        args += ["--parent-id", parent_id]
    if tags:
        args += ["--tags", ",".join(tags)]
    if sort_order is not None:
        args += ["--sort-order", str(sort_order)]
    result = run_dida(args)
    if not isinstance(result, dict) or not result.get("id"):
        raise DidaError(f"创建任务没有返回 id: {result!r}")
    return result


def update_task(project_id: str, task_id: str, **fields) -> dict:
    args = [
        "task",
        "update",
        task_id,
        "--id",
        task_id,
        "--project",
        project_id,
        "--json",
    ]
    title = fields.get("title")
    content = fields.get("content")
    status = fields.get("status")
    tags = fields.get("tags")
    if title is not None:
        args += ["--title", title]
    if content is not None:
        args += ["--content", content]
    if status is not None:
        args += ["--status", str(status)]
    if tags is not None:
        # A comma parses to an empty list; the CLI does not send empty strings.
        args += ["--tags", ",".join(tags) if tags else ","]
    result = run_dida(args)
    return result if isinstance(result, dict) else {}


def complete_task(project_id: str, task_id: str) -> None:
    run_dida(["task", "complete", project_id, task_id], json_expected=False)


def task_status(task: dict) -> str:
    if int(task.get("status", 0)) == 2:
        return "complete"
    if int(task.get("status", 0)) == -1:
        return "pending"
    tags = task.get("tags") or []
    if IN_PROGRESS_TAG in tags:
        return "in_progress"
    return "pending"


def child_ids(parent: dict) -> list[str]:
    values = parent.get("childIds") or []
    return [str(value) for value in values if value]


def fetch_children(project_id: str, parent: dict) -> list[dict]:
    children = []
    for task_id in child_ids(parent):
        task = get_task(project_id, task_id)
        if task is not None:
            children.append(task)
    return children


def search_tasks(project_id: str, keyword: str) -> list[dict]:
    result = run_dida(
        [
            "task",
            "search",
            keyword,
            "--projects",
            project_id,
            "--json",
        ]
    )
    return result if isinstance(result, list) else []


def find_task(
    project_id: str,
    title: str,
    plan_rel: str,
    parent_id: Optional[str] = None,
) -> Optional[dict]:
    matches = []
    for task in search_tasks(project_id, title):
        if task.get("title") != title:
            continue
        content = str(task.get("content") or "")
        if plan_rel not in content:
            continue
        if parent_id is not None and str(task.get("parentId") or "") != parent_id:
            continue
        if parent_id is None and task.get("parentId"):
            continue
        matches.append(task)
    return matches[0] if matches else None


def fetch_remote(
    project_id: str, task_ids: list[str], parent_id: Optional[str] = None
) -> dict[str, dict]:
    found: dict[str, dict] = {}
    active = run_dida(["project", "data", project_id, "--json"])
    if isinstance(active, dict):
        for task in active.get("tasks") or []:
            if task.get("id"):
                found[str(task["id"])] = task
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    completed = run_dida(
        [
            "task",
            "completed",
            "--projects",
            project_id,
            "--start-date",
            "2020-01-01T00:00:00Z",
            "--end-date",
            tomorrow,
            "--json",
        ]
    )
    if isinstance(completed, list):
        for task in completed:
            if task.get("id"):
                found[str(task["id"])] = task
    for task_id in [*task_ids, *([parent_id] if parent_id else [])]:
        if task_id and task_id not in found:
            task = get_task(project_id, task_id)
            if task is not None:
                found[task_id] = task
    return found


def attest_if_needed(plan_path: Path) -> None:
    attest = Path(__file__).resolve().parent / "attest-plan.sh"
    if not attest.is_file():
        return
    plan_dir = plan_path.parent
    target = None
    if (plan_dir / ".attestation").is_file():
        target = plan_dir.name
    elif (plan_dir / ".plan-attestation").is_file():
        target = "root"
    if not target:
        return
    env = dict(os.environ)
    env.pop("PLAN_ID", None)
    try:
        subprocess.run(
            ["sh", str(attest), "--target", target],
            cwd=str(Path.cwd()),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def validate_for_init(
    goal: str, next_step: str, phases: list[Phase], force: bool
) -> None:
    if not phases:
        raise PlanError("plan 中没有阶段")
    if len(phases) > 20:
        raise PlanError("阶段超过 20 个；拆成多个 plan 再同步滴答")
    if not force:
        if not goal or goal.startswith("["):
            raise PlanError("先填写 ## Goal（目标），再同步滴答；可加 --force 跳过")
        if not next_step or next_step.startswith("["):
            raise PlanError("先填写 ## Next Step（下一步），再同步滴答；可加 --force 跳过")


def cmd_init(args) -> int:
    plan_path = resolve_plan(args.plan)
    if plan_path is None:
        raise PlanError("找不到 task_plan.md")
    lines, phases, goal, next_step = parse_plan(read_text(plan_path))
    validate_for_init(goal, next_step, phases, args.force)

    sidecar = load_sidecar(plan_path)
    project_id = project_id_for(args.project, sidecar)
    name = args.name or derive_name(plan_path, lines)
    title = parent_title(name)
    plan_rel = plan_relative(plan_path)
    content = parent_content(plan_path, goal, next_step, phases)

    parent = None
    parent_id = str(sidecar.get("parentId") or "")
    if parent_id:
        parent = get_task(project_id, parent_id)
    if parent is None:
        parent = find_task(project_id, title, plan_rel)
    if parent is not None and int(parent.get("status", 0)) == 2 and not args.force:
        raise PlanError(
            "滴答里的父任务已完成；先在滴答中重新打开它，或加 --force 新建父任务"
        )

    if parent is None:
        parent = create_task(project_id, title, content)
    else:
        if args.force and int(parent.get("status", 0)) == 2:
            update_task(project_id, str(parent["id"]), status=0)
        update_task(project_id, str(parent["id"]), title=title, content=content)

    parent_id = str(parent["id"])
    existing_children = fetch_children(project_id, parent)
    by_number: dict[int, dict] = {}
    for task in existing_children:
        match = PHASE_PREFIX_RE.match(str(task.get("title") or ""))
        if match:
            by_number[int(match.group(1))] = task

    old_map = sidecar.get("phases") if isinstance(sidecar.get("phases"), dict) else {}
    mapping: dict[str, str] = {}
    for phase in phases:
        task = None
        old_id = str(old_map.get(str(phase.number)) or "")
        if old_id:
            task = get_task(project_id, old_id)
        if task is None:
            task = by_number.get(phase.number)
        if task is None:
            task = find_task(
                project_id,
                phase_title_remote(phase.number, phase.title),
                plan_rel,
                parent_id,
            )

        tags = [IN_PROGRESS_TAG] if phase.status == "in_progress" else []
        if task is None:
            task = create_task(
                project_id,
                phase_title_remote(phase.number, phase.title),
                child_content(plan_path, phase.number, len(phases)),
                parent_id=parent_id,
                tags=tags,
                sort_order=phase.number * 10,
            )
            if phase.status == "complete":
                complete_task(project_id, str(task["id"]))
        else:
            update_task(
                project_id,
                str(task["id"]),
                title=phase_title_remote(phase.number, phase.title),
                content=child_content(plan_path, phase.number, len(phases)),
                status=0 if phase.status != "complete" else 2,
                tags=tags,
            )
        mapping[str(phase.number)] = str(task["id"])

    statuses = {phase.number: phase.status for phase in phases}
    update_task(
        project_id,
        parent_id,
        content=parent_content(plan_path, goal, next_step, phases, statuses),
    )

    sidecar = {
        "version": 1,
        "plan": plan_rel,
        "projectId": project_id,
        "parentId": parent_id,
        "parentTitle": title,
        "phases": mapping,
        "lastPull": now_iso(),
        "lastPush": now_iso(),
    }
    save_sidecar(plan_path, sidecar)
    attest_if_needed(plan_path)
    print(f"已同步到滴答：{title}（{len(mapping)} 个阶段，父任务 {parent_id}）")
    return 0


def max_age_allows_pull(sidecar: dict, max_age: int, force: bool) -> bool:
    if force or max_age <= 0:
        return True
    last = parse_iso(str(sidecar.get("lastPull") or ""))
    if last is None:
        return True
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age >= max_age


def cmd_pull(args) -> int:
    plan_path = resolve_plan(args.plan)
    if plan_path is None:
        return 0
    sidecar = load_sidecar(plan_path)
    if not sidecar or not sidecar.get("phases"):
        return 0
    if not max_age_allows_pull(sidecar, args.max_age, args.force):
        return 0

    project_id = str(sidecar.get("projectId") or "")
    parent_id = str(sidecar.get("parentId") or "")
    if not project_id:
        if args.quiet:
            return 0
        raise PlanError("sidecar 缺少 projectId")

    try:
        mapping = sidecar.get("phases") or {}
        task_ids = [str(value) for value in mapping.values() if value]
        remote = fetch_remote(project_id, task_ids, parent_id or None)
    except DidaError as exc:
        if args.quiet:
            return 0
        print(f"[planning-with-dida] 拉取滴答状态失败：{exc}", file=sys.stderr)
        return 1

    lines, phases, _, _ = parse_plan(read_text(plan_path))
    parent = remote.get(parent_id) if parent_id else None
    parent_complete = bool(parent and int(parent.get("status", 0)) == 2)

    remote_status: dict[int, str] = {}
    remote_titles: dict[int, str] = {}
    warnings = 0
    for phase in phases:
        task_id = str(mapping.get(str(phase.number)) or "")
        task = remote.get(task_id) if task_id else None
        if task is None:
            remote_status[phase.number] = phase.status
            warnings += 1
            continue
        remote_status[phase.number] = (
            "complete" if parent_complete else task_status(task)
        )
        title = str(task.get("title") or "")
        if title:
            clean_title = PHASE_PREFIX_RE.sub("", title).strip()
            if clean_title:
                remote_titles[phase.number] = clean_title

    if parent_complete:
        remote_status = {phase.number: "complete" for phase in phases}

    new_text = render_plan(lines, phases, remote_titles, remote_status)
    changed = write_text_atomic(plan_path, new_text)

    sidecar["lastPull"] = now_iso()
    sidecar["lastKnown"] = {
        str(key): value for key, value in remote_status.items()
    }
    save_sidecar(plan_path, sidecar)
    if changed:
        attest_if_needed(plan_path)

    if not args.quiet:
        complete = sum(1 for value in remote_status.values() if value == "complete")
        print(
            f"已从滴答拉取：{complete}/{len(phases)} 完成"
            + (f"，{warnings} 个阶段找不到对应任务" if warnings else "")
        )
    return 0


def cmd_phase(args) -> int:
    plan_path = resolve_plan(args.plan)
    if plan_path is None:
        raise PlanError("找不到 task_plan.md")
    sidecar = load_sidecar(plan_path)
    if not sidecar or not sidecar.get("phases"):
        raise PlanError("这个 plan 还没同步滴答；先运行 dida-sync.py init")
    mapping = sidecar.get("phases") or {}
    task_id = str(mapping.get(str(args.phase)) or "")
    if not task_id:
        raise PlanError(f"阶段 {args.phase} 没有对应的滴答任务；先运行 sync")

    project_id = str(sidecar.get("projectId") or "")
    parent_id = str(sidecar.get("parentId") or "")
    lines, phases, goal, next_step = parse_plan(read_text(plan_path))
    if not any(phase.number == args.phase for phase in phases):
        raise PlanError(f"plan 中没有阶段 {args.phase}")

    if args.status == "complete":
        complete_task(project_id, task_id)
    else:
        tags = [IN_PROGRESS_TAG] if args.status == "in_progress" else []
        update_task(project_id, task_id, status=0, tags=tags)

    statuses = {phase.number: phase.status for phase in phases}
    statuses[int(args.phase)] = args.status
    if parent_id:
        update_task(
            project_id,
            parent_id,
            content=parent_content(plan_path, goal, next_step, phases, statuses),
        )
        all_complete = all(status == "complete" for status in statuses.values())
        if all_complete:
            complete_task(project_id, parent_id)
        else:
            update_task(project_id, parent_id, status=0)

    new_text = render_plan(lines, phases, {}, {int(args.phase): args.status})
    write_text_atomic(plan_path, new_text)
    attest_if_needed(plan_path)
    print(f"阶段 {args.phase} → {args.status}")
    return 0


def cmd_next(args) -> int:
    plan_path = resolve_plan(args.plan)
    if plan_path is None:
        raise PlanError("找不到 task_plan.md")
    text = read_text(plan_path)
    lines, phases, goal, _ = parse_plan(text)
    value = clean_text(args.text, 300)
    if not replace_section(lines, "Next Step", value):
        raise PlanError("plan 中没有 ## Next Step 段")
    write_text_atomic(plan_path, "".join(lines))
    attest_if_needed(plan_path)

    sidecar = load_sidecar(plan_path)
    if sidecar.get("parentId"):
        project_id = str(sidecar.get("projectId") or "")
        statuses = (
            sidecar.get("lastKnown")
            if isinstance(sidecar.get("lastKnown"), dict)
            else None
        )
        mapped = (
            {int(key): str(value) for key, value in statuses.items()}
            if statuses
            else None
        )
        update_task(
            project_id,
            str(sidecar["parentId"]),
            content=parent_content(plan_path, goal, value, phases, mapped),
        )
    print(f"下一步 → {value}")
    return 0


def cmd_complete(args) -> int:
    plan_path = resolve_plan(args.plan)
    if plan_path is None:
        raise PlanError("找不到 task_plan.md")
    sidecar = load_sidecar(plan_path)
    if not sidecar or not sidecar.get("phases"):
        raise PlanError("这个 plan 还没同步滴答；先运行 dida-sync.py init")
    project_id = str(sidecar.get("projectId") or "")
    parent_id = str(sidecar.get("parentId") or "")

    for task_id in (sidecar.get("phases") or {}).values():
        if not task_id:
            continue
        task = get_task(project_id, str(task_id))
        if task is not None and int(task.get("status", 0)) != 2:
            try:
                complete_task(project_id, str(task_id))
            except DidaError as exc:
                print(f"跳过无法完成的任务 {task_id}: {exc}", file=sys.stderr)
    lines, phases, goal, next_step = parse_plan(read_text(plan_path))
    statuses = {phase.number: "complete" for phase in phases}
    if parent_id:
        update_task(
            project_id,
            parent_id,
            content=parent_content(plan_path, goal, next_step, phases, statuses),
        )
        parent = get_task(project_id, parent_id)
        if parent is not None and int(parent.get("status", 0)) != 2:
            complete_task(project_id, parent_id)

    new_text = render_plan(lines, phases, {}, statuses)
    write_text_atomic(plan_path, new_text)
    attest_if_needed(plan_path)
    print(f"已完成 {len(phases)} 个阶段" + ("和父任务" if parent_id else ""))
    return 0


def cmd_status(args) -> int:
    plan_path = resolve_plan(args.plan)
    if plan_path is None:
        raise PlanError("找不到 task_plan.md")
    sidecar = load_sidecar(plan_path)
    if not sidecar:
        raise PlanError("这个 plan 没有 .dida.json；先运行 init")
    project_id = str(sidecar.get("projectId") or "")
    parent_id = str(sidecar.get("parentId") or "")
    mapping = sidecar.get("phases") or {}
    remote = fetch_remote(
        project_id,
        [str(value) for value in mapping.values()],
        parent_id or None,
    )
    parent = remote.get(parent_id) if parent_id else None
    rows = []
    for number, task_id in sorted(
        mapping.items(), key=lambda item: int(item[0])
    ):
        task = remote.get(str(task_id)) or {}
        rows.append(
            {
                "phase": int(number),
                "taskId": str(task_id),
                "title": task.get("title") or "",
                "status": task_status(task) if task else "missing",
            }
        )
    payload = {
        "plan": str(plan_path),
        "projectId": project_id,
        "parentId": parent_id,
        "parentTitle": (parent or {}).get("title")
        or sidecar.get("parentTitle")
        or "",
        "parentStatus": (
            "complete"
            if parent and int(parent.get("status", 0)) == 2
            else "open"
        )
        if parent
        else "missing",
        "phases": rows,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"plan: {payload['plan']}")
    print(f"parent: {payload['parentTitle']} ({payload['parentStatus']}) {parent_id}")
    for row in rows:
        print(f"  P{row['phase']}  {row['status']:<11} {row['title']}  {row['taskId']}")
    return 0


def restore_target(explicit: Optional[str]) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate.resolve()
    resolved = resolve_plan(None)
    if resolved is not None:
        return resolved
    return Path.cwd() / PLAN_FILENAME


def cmd_restore(args) -> int:
    plan_path = restore_target(args.plan)
    if plan_path.exists() and not args.force:
        raise PlanError("task_plan.md 已存在；加 --force 才会覆盖")
    sidecar = load_sidecar(plan_path)
    project_id = str(sidecar.get("projectId") or "") or args.project or ""
    if not project_id:
        raise PlanError("缺少 projectId；传 --project <id> 或保留 .dida.json")
    parent_id = str(sidecar.get("parentId") or "") or args.parent or ""
    if not parent_id:
        raise PlanError("缺少父任务 ID；传 --parent <taskId> 或保留 .dida.json")
    parent = get_task(project_id, parent_id)
    if parent is None:
        raise PlanError(f"父任务不存在: {parent_id}")

    children = fetch_children(project_id, parent)
    numbered = []
    for task in children:
        match = PHASE_PREFIX_RE.match(str(task.get("title") or ""))
        number = int(match.group(1)) if match else len(numbered) + 1
        numbered.append((number, task))
    numbered.sort(key=lambda item: item[0])

    content = str(parent.get("content") or "")
    goal = ""
    next_step = ""
    for line in content.splitlines():
        if line.startswith("目标："):
            goal = line[3:].strip()
        elif line.startswith("下一步："):
            next_step = line[4:].strip()

    statuses = {number: task_status(task) for number, task in numbered}
    phases_text = []
    for number, task in numbered:
        title = (
            PHASE_PREFIX_RE.sub("", str(task.get("title") or "")).strip()
            or f"Phase {number}"
        )
        phases_text.append(
            f"### Phase {number}: {title}\n"
            f"- [ ] (synced from Dida)\n"
            f"- **Status:** {statuses[number]}\n"
        )
    in_progress = [number for number in statuses if statuses[number] == "in_progress"]
    pending = [number for number in statuses if statuses[number] == "pending"]
    current = (
        f"Phase {in_progress[0]}"
        if in_progress
        else (f"Phase {pending[0]}" if pending else "All phases complete")
    )
    title = str(parent.get("title") or "")
    if title.startswith(PLAN_PREFIX):
        title = title[len(PLAN_PREFIX) :]
    text = (
        f"# Task Plan: {title}\n\n"
        f"## Goal\n{goal or '（未填写）'}\n\n"
        f"## Next Step\n{next_step or '（未填写）'}\n\n"
        f"## Current Phase\n{current}\n\n"
        f"## Phases\n\n"
        + "\n".join(phases_text)
        + "\n## Decisions Made\n| Decision | Rationale |\n|----------|-----------|\n\n"
        + "## Errors Encountered\n| Error | Resolution |\n|-------|------------|\n"
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(text, encoding="utf-8")
    mapping = {str(number): str(task["id"]) for number, task in numbered}
    save_sidecar(
        plan_path,
        {
            "version": 1,
            "plan": plan_relative(plan_path),
            "projectId": project_id,
            "parentId": parent_id,
            "parentTitle": str(parent.get("title") or ""),
            "phases": mapping,
            "lastPull": now_iso(),
        },
    )
    print(f"已从滴答恢复 {plan_path}（{len(numbered)} 个阶段）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dida-sync.py",
        description="Sync a planning-with-dida plan with Dida365.",
    )
    parser.add_argument("--plan", help="path to task_plan.md (default: active plan)")
    parser.add_argument("--project", help="Dida project id (default: Project list)")
    parser.add_argument("--json", action="store_true", help="JSON output (status)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create/update parent and phase tasks")
    init.add_argument("--name", help="plan name used in the parent task title")
    init.add_argument(
        "--force", action="store_true", help="create even if validation fails"
    )
    init.set_defaults(func=cmd_init)

    sync = sub.add_parser("sync", help="alias for init")
    sync.add_argument("--name", help="plan name used in the parent task title")
    sync.add_argument(
        "--force", action="store_true", help="create even if validation fails"
    )
    sync.set_defaults(func=cmd_init)

    pull = sub.add_parser("pull", help="pull phase statuses from Dida")
    pull.add_argument("--quiet", action="store_true")
    pull.add_argument("--force", action="store_true")
    pull.add_argument(
        "--max-age", type=int, default=0, help="skip if pulled this recently"
    )
    pull.set_defaults(func=cmd_pull)

    phase = sub.add_parser("phase", help="set one phase status")
    phase.add_argument("phase", type=int)
    phase.add_argument("status", choices=["pending", "in_progress", "complete"])
    phase.set_defaults(func=cmd_phase)

    nxt = sub.add_parser("next", help="set the next step")
    nxt.add_argument("text")
    nxt.set_defaults(func=cmd_next)

    complete = sub.add_parser("complete", help="complete all phases and the parent")
    complete.set_defaults(func=cmd_complete)

    status = sub.add_parser("status", help="show the Dida mapping and state")
    status.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="JSON output",
    )
    status.set_defaults(func=cmd_status)

    restore = sub.add_parser("restore", help="rebuild task_plan.md from Dida")
    restore.add_argument("--parent", help="parent task id")
    restore.add_argument("--force", action="store_true")
    restore.set_defaults(func=cmd_restore)

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except PlanError as exc:
        print(f"[planning-with-dida] {exc}", file=sys.stderr)
        return 2
    except DidaError as exc:
        print(f"[planning-with-dida] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
