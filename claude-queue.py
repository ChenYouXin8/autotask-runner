#!/usr/bin/env python3
"""
Task Queue Manager — dual-engine fork (Antigravity `agy` + OpenAI `codex`)

Handles batch execution of AI coding tasks with:
  * two interchangeable CLI engines with automatic failover
    (attempt 1 -> `agy`, retries -> `codex`),
  * an external hard verifier (`verify_commands`) that must exit 0,
  * a git guard that auto-commits verified work and rolls back skipped tasks.

Usage:
    # Add tasks
    ./claude-queue.py add "Refactor authentication module" --session auth-refactor
    ./claude-queue.py add "Fix the failing tests" --verify-command "pytest" --on-fail skip

    # Batch load a task book
    ./claude-queue.py batch task_book_demo.yaml

    # Start worker
    ./claude-queue.py worker

    # Check status / list all tasks
    ./claude-queue.py status
    ./claude-queue.py list
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

# Cross-platform advisory file locking.
# fcntl only exists on POSIX; Windows falls back to a best-effort msvcrt lock.
try:
    import fcntl as _fcntl

    fcntl: Any = _fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt as _msvcrt

    msvcrt: Any = _msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

# Optional YAML support for batch loading
try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("claude-queue")


# Custom exceptions
class QueueError(Exception):
    """Base exception for queue operations"""

    pass


class ValidationError(QueueError):
    """Raised when input validation fails"""

    pass


class QueueFileError(QueueError):
    """Raised when queue file operations fail"""

    pass


def _acquire_lock(file_obj, exclusive: bool) -> None:
    """Acquire an advisory lock (no-op when the platform cannot provide one)."""
    if fcntl is not None:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    elif msvcrt is not None:
        try:
            file_obj.seek(0)
            msvcrt.locking(file_obj.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:  # pragma: no cover - locking is best effort on Windows
            logger.debug("msvcrt locking unavailable; continuing without a lock")


def _release_lock(file_obj) -> None:
    """Release an advisory lock previously taken with :func:`_acquire_lock`."""
    if fcntl is not None:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        try:
            file_obj.seek(0)
            msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# CLI engine abstraction
#
# The original project hard-coded the `claude` CLI. This fork decouples the
# executor into interchangeable engines with automatic failover:
#   "agy"    -> Antigravity CLI    (attempt 1 when installed)
#   "gemini" -> Google Gemini CLI  (attempt 1 when installed)
#   "codex"  -> OpenAI Codex CLI   (retry / default everywhere)
#
# Defaults are both "codex": `agy` and `gemini` are not registered on PATH on
# every machine, and silently burning attempt 1 on a missing binary would waste
# a retry. Opt in with --primary-engine agy (or gemini) when available.
# ---------------------------------------------------------------------------


class EngineError(QueueError):
    """Raised when an unknown engine is requested or cannot be resolved."""

    pass


DEFAULT_PRIMARY_ENGINE = "codex"
DEFAULT_FALLBACK_ENGINE = "codex"

# Bare binary name resolved through shutil.which (honours PATHEXT on Windows).
ENGINE_BINARIES: dict[str, str] = {
    "agy": "agy",
    "gemini": "gemini",
    "codex": "codex",
}

# Optional env var to override the resolved executable (useful on Windows).
ENGINE_BIN_ENV: dict[str, str] = {
    "agy": "AGY_BIN",
    "gemini": "GEMINI_BIN",
    "codex": "CODEX_BIN",
}


def build_engine_command(engine: str, prompt: str, binary: str) -> list[str]:
    """Build the argv for a given engine.

    - agy:    [agy, -p, {prompt}, --dangerously-skip-permissions]
    - gemini: [gemini, -p, {prompt}, --yolo]
    - codex:  [codex, exec, --ask-for-approval, never, --sandbox,
               workspace-write, {prompt}]
    """
    if engine == "agy":
        return [binary, "-p", prompt, "--dangerously-skip-permissions"]
    if engine == "gemini":
        # `--yolo` = non-interactive auto-approve, the Gemini CLI equivalent of
        # agy's --dangerously-skip-permissions.
        return [binary, "-p", prompt, "--yolo"]
    if engine == "codex":
        return [
            binary,
            "exec",
            "--ask-for-approval",
            "never",
            "--sandbox",
            "workspace-write",
            prompt,
        ]
    raise EngineError(f"Unknown engine: {engine}")


def resolve_engine_binary(engine: str) -> str | None:
    """Resolve an engine executable in a Windows-compatible way.

    Precedence:
      1. Explicit override via env var (AGY_BIN / CODEX_BIN), used as-is when
         the path exists, otherwise passed through shutil.which.
      2. shutil.which() lookup, which honours PATHEXT (.exe/.cmd/.bat) on Windows.

    Returns None when the binary cannot be found.
    """
    env_name = ENGINE_BIN_ENV.get(engine)
    override = (os.getenv(env_name, "") if env_name else "").strip()
    if override:
        override_path = Path(override).expanduser()
        if override_path.exists():
            return str(override_path)
        resolved = shutil.which(override)
        if resolved:
            return resolved
        logger.warning(f"{env_name}='{override}' could not be resolved")

    return shutil.which(ENGINE_BINARIES.get(engine, engine))


def select_engine(attempt: int, primary: str, fallback: str) -> str:
    """Return the engine to use for a 1-based attempt number.

    Attempt 1 uses the primary engine (agy); attempt 2+ switches to the
    fallback engine (codex), giving a retry a genuinely different executor.
    """
    return primary if attempt <= 1 else fallback


class TaskStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    SKIPPED = "skipped"


@dataclass
class Task:
    id: str
    prompt: str
    session_name: str | None
    status: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    last_error: str | None = None
    priority: int = 0  # Higher = higher priority
    depends_on: list[str] | None = None  # List of task IDs this task depends on
    working_dir: str | None = None  # Directory where task should execute
    timeout: int | None = None  # Per-task timeout in seconds; None = use worker default
    verify_commands: list[str] | None = None  # Commands that must ALL exit 0 to pass
    on_fail: str = "fail"  # "fail" (default) or "skip" (rollback + STUCK.md)
    feedback: str = ""  # Verifier feedback appended to the prompt on retry
    engine: str | None = None  # Engine used for the most recent attempt
    commit_hash: str | None = None  # Git commit created when the task verified OK

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


class TaskQueue:
    """Manages task queue using JSON file storage with file locking"""

    def __init__(self, queue_file: Path):
        self.queue_file = queue_file
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_queue_exists()

    def _ensure_queue_exists(self):
        if not self.queue_file.exists():
            self._save_tasks([])

    @staticmethod
    def _validate_prompt(prompt: str) -> None:
        """Validate task prompt"""
        if not prompt or not prompt.strip():
            raise ValidationError("Prompt cannot be empty")
        if len(prompt) > 10000:
            raise ValidationError("Prompt too long (max 10000 characters)")

    @staticmethod
    def _validate_session_name(session_name: str | None) -> None:
        """Validate session name"""
        if session_name is not None:
            if not session_name.strip():
                raise ValidationError("Session name cannot be empty string")
            if not session_name.replace("-", "").replace("_", "").isalnum():
                raise ValidationError(
                    "Session name must contain only alphanumeric characters, hyphens, and underscores"
                )
            if len(session_name) > 100:
                raise ValidationError("Session name too long (max 100 characters)")

    @staticmethod
    def _validate_priority(priority: int) -> None:
        """Validate priority value"""
        if not isinstance(priority, int):
            raise ValidationError("Priority must be an integer")
        if priority < 0 or priority > 100:
            raise ValidationError("Priority must be between 0 and 100")

    @staticmethod
    def _validate_max_attempts(max_attempts: int) -> None:
        """Validate max_attempts value"""
        if not isinstance(max_attempts, int):
            raise ValidationError("max_attempts must be an integer")
        if max_attempts < 1 or max_attempts > 100:
            raise ValidationError("max_attempts must be between 1 and 100")

    @staticmethod
    def _validate_working_dir(working_dir: str | None) -> None:
        """Validate working_dir exists"""
        if working_dir is not None:
            working_path = Path(working_dir).expanduser().resolve()
            if not working_path.exists():
                raise ValidationError(f"Working directory does not exist: {working_dir}")
            if not working_path.is_dir():
                raise ValidationError(f"Working directory is not a directory: {working_dir}")

    @staticmethod
    def _validate_verify_commands(verify_commands: list[str] | None) -> None:
        """Validate the optional external verifier command list"""
        if verify_commands is None:
            return
        if isinstance(verify_commands, str):
            raise ValidationError("verify_commands must be a list of command strings")
        if not isinstance(verify_commands, list):
            raise ValidationError("verify_commands must be a list of command strings")
        for command in verify_commands:
            if not isinstance(command, str) or not command.strip():
                raise ValidationError("Each verify command must be a non-empty string")

    @staticmethod
    def _validate_on_fail(on_fail: str) -> None:
        """Validate the on_fail policy"""
        if on_fail not in ("fail", "skip"):
            raise ValidationError("on_fail must be either 'fail' or 'skip'")

    def _validate_dependencies(
        self,
        depends_on: list[str] | None,
        task_id: str | None = None,
        existing_tasks: list[Task] | None = None,
    ) -> None:
        """Validate task dependencies"""
        if not depends_on:
            return

        if not isinstance(depends_on, list):
            raise ValidationError("depends_on must be a list of task IDs")

        if not all(isinstance(dep, str) for dep in depends_on):
            raise ValidationError("All dependency IDs must be strings")

        # Check for self-dependency
        if task_id and task_id in depends_on:
            raise ValidationError("Task cannot depend on itself")

        # Use pre-loaded tasks if provided to avoid a second file read
        if existing_tasks is None:
            existing_tasks = self._load_tasks()
        existing_ids = {task.id for task in existing_tasks}

        for dep_id in depends_on:
            if dep_id not in existing_ids:
                raise ValidationError(f"Dependency task '{dep_id}' does not exist")

        # Check for circular dependencies
        if task_id:
            self._check_circular_dependencies(task_id, depends_on, existing_tasks)

    def _check_circular_dependencies(
        self, task_id: str, depends_on: list[str], all_tasks: list[Task]
    ) -> None:
        """Check for circular dependencies using DFS"""
        task_map = {task.id: task for task in all_tasks}
        visited = set()
        rec_stack = set()

        def has_cycle(current_id: str) -> bool:
            if current_id in rec_stack:
                return True
            if current_id in visited:
                return False

            visited.add(current_id)
            rec_stack.add(current_id)

            # Check dependencies of current task
            if current_id in task_map:
                current_deps = task_map[current_id].depends_on or []
                for dep in current_deps:
                    if has_cycle(dep):
                        return True
            # Check new dependencies we're adding
            elif current_id == task_id:
                for dep in depends_on:
                    if has_cycle(dep):
                        return True

            rec_stack.remove(current_id)
            return False

        if has_cycle(task_id):
            raise ValidationError("Circular dependency detected")

    def _dependencies_satisfied(self, task: Task, all_tasks: list[Task]) -> bool:
        """Check if all dependencies of a task are completed"""
        if not task.depends_on:
            return True

        task_map = {t.id: t for t in all_tasks}

        for dep_id in task.depends_on:
            dep_task = task_map.get(dep_id)
            if not dep_task:
                # Dependency doesn't exist (maybe deleted), allow task to run
                logger.warning(f"Task {task.id} depends on non-existent task {dep_id}")
                continue
            if dep_task.status != TaskStatus.COMPLETED.value:
                return False

        return True

    def _load_tasks(self) -> list[Task]:
        """Load tasks from file with error handling and file locking"""
        try:
            with open(self.queue_file) as f:
                # Acquire shared lock for reading
                _acquire_lock(f, exclusive=False)
                try:
                    data = json.load(f)
                    if not isinstance(data, list):
                        raise QueueFileError("Invalid queue format: expected list")
                    return [Task.from_dict(t) for t in data]
                finally:
                    _release_lock(f)
        except FileNotFoundError:
            logger.warning(f"Queue file not found: {self.queue_file}, creating new queue")
            self._ensure_queue_exists()
            return []
        except json.JSONDecodeError as e:
            logger.error(f"Corrupted queue file: {e}")
            # Backup corrupted file
            backup_path = self.queue_file.with_suffix(".json.backup")
            shutil.copy(self.queue_file, backup_path)
            logger.info(f"Backed up corrupted queue to: {backup_path}")
            raise QueueFileError(f"Corrupted queue file (backed up to {backup_path})") from e
        except Exception as e:
            logger.error(f"Error loading tasks: {e}")
            raise QueueFileError(f"Failed to load tasks: {e}") from e

    def _save_tasks(self, tasks: list[Task]) -> None:
        """Atomically save tasks with file locking"""
        temp_file = None
        try:
            # Write to temporary file first
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.queue_file.parent,
                prefix=".tasks_tmp_",
                suffix=".json",
                delete=False,
            ) as f:
                temp_file = Path(f.name)
                # Acquire exclusive lock for writing
                _acquire_lock(f, exclusive=True)
                try:
                    json.dump([t.to_dict() for t in tasks], f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())  # Ensure written to disk
                finally:
                    _release_lock(f)

            # Atomic rename
            temp_file.replace(self.queue_file)
            logger.debug(f"Successfully saved {len(tasks)} tasks")
        except Exception as e:
            logger.error(f"Error saving tasks: {e}")
            if temp_file and temp_file.exists():
                temp_file.unlink()
            raise QueueFileError(f"Failed to save tasks: {e}") from e

    def add_task(
        self,
        prompt: str,
        session_name: str | None = None,
        max_attempts: int = 3,
        priority: int = 0,
        depends_on: list[str] | None = None,
        working_dir: str | None = None,
        timeout: int | None = None,
        verify_commands: list[str] | None = None,
        on_fail: str = "fail",
    ) -> Task:
        # Validate inputs
        self._validate_prompt(prompt)
        self._validate_session_name(session_name)
        self._validate_priority(priority)
        self._validate_max_attempts(max_attempts)
        self._validate_working_dir(working_dir)
        self._validate_verify_commands(verify_commands)
        self._validate_on_fail(on_fail)

        tasks = self._load_tasks()

        # Generate unique ID
        task_id = f"task-{uuid.uuid4().hex[:12]}"

        # Validate dependencies after generating task_id (reuse already-loaded tasks)
        self._validate_dependencies(depends_on, task_id, existing_tasks=tasks)

        task = Task(
            id=task_id,
            prompt=prompt,
            session_name=session_name or task_id,
            status=TaskStatus.QUEUED.value,
            created_at=datetime.now().isoformat(),
            max_attempts=max_attempts,
            priority=priority,
            depends_on=depends_on,
            working_dir=working_dir,
            timeout=timeout,
            verify_commands=verify_commands or None,
            on_fail=on_fail,
        )

        tasks.append(task)
        self._save_tasks(tasks)

        dep_info = f" (depends on: {', '.join(depends_on)})" if depends_on else ""
        logger.info(f"Added task {task_id} with priority {priority}{dep_info}")

        return task

    def get_next_task(self) -> Task | None:
        """Get next queued task (highest priority first, dependencies satisfied)"""
        tasks = self._load_tasks()

        queued_tasks = [
            t
            for t in tasks
            if t.status in [TaskStatus.QUEUED.value, TaskStatus.RATE_LIMITED.value]
            and t.attempts < t.max_attempts
            and self._dependencies_satisfied(t, tasks)  # Check dependencies
        ]

        if not queued_tasks:
            return None

        # Sort by priority (desc) then created_at (asc)
        queued_tasks.sort(key=lambda t: (-t.priority, t.created_at))
        return queued_tasks[0]

    def update_task(self, task_id: str, **updates) -> None:
        tasks = self._load_tasks()

        for task in tasks:
            if task.id == task_id:
                for key, value in updates.items():
                    setattr(task, key, value)
                break
        else:
            raise QueueError(f"Task {task_id} not found")

        self._save_tasks(tasks)

    def get_all_tasks(self) -> list[Task]:
        return self._load_tasks()

    def remove_task(self, task_id: str):
        tasks = self._load_tasks()
        tasks = [t for t in tasks if t.id != task_id]
        self._save_tasks(tasks)

    def clear_completed(self):
        """Remove completed tasks"""
        tasks = self._load_tasks()
        tasks = [t for t in tasks if t.status != TaskStatus.COMPLETED.value]
        self._save_tasks(tasks)

    def get_stats(self):
        tasks = self._load_tasks()
        counts = Counter(t.status for t in tasks)
        return {
            "total": len(tasks),
            "queued": counts[TaskStatus.QUEUED.value],
            "running": counts[TaskStatus.RUNNING.value],
            "completed": counts[TaskStatus.COMPLETED.value],
            "failed": counts[TaskStatus.FAILED.value],
            "rate_limited": counts[TaskStatus.RATE_LIMITED.value],
            "skipped": counts[TaskStatus.SKIPPED.value],
        }


class ClaudeWorker:
    """Executes tasks from queue with retry logic and a two-engine executor.

    Retained the original class name for backwards compatibility with the
    project's test-suite; the implementation no longer shells out to `claude`.
    """

    def __init__(
        self,
        queue: TaskQueue,
        base_retry_delay: int = 60,
        save_output: bool = False,
        output_dir: Path | None = None,
        stream_output: bool = False,
        idle: bool = False,
        idle_interval: int = 30,
        task_timeout: int = 3600,
        primary_engine: str = DEFAULT_PRIMARY_ENGINE,
        fallback_engine: str = DEFAULT_FALLBACK_ENGINE,
        verify_timeout: int = 600,
        enable_git_guard: bool = True,
        report_file: Path | None = None,
    ):
        self.queue = queue
        self.base_retry_delay = base_retry_delay
        self.running = True
        self.save_output = save_output
        self.stream_output = stream_output
        self.idle = idle
        self.idle_interval = idle_interval
        self.task_timeout = task_timeout
        self.primary_engine = primary_engine
        self.fallback_engine = fallback_engine
        self.verify_timeout = verify_timeout
        self.enable_git_guard = enable_git_guard
        # Morning report: written to the queue run directory by default.
        self.report_file = report_file or (Path.cwd() / "MORNING_REPORT.md")
        self.started_at: datetime | None = None
        self.output_dir = output_dir or (Path.home() / ".claude-queue" / "outputs")

        if self.save_output:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def parse_rate_limit_info(self, stderr: str) -> dict[str, int | str | None]:
        """
        Parse rate limit information from engine CLI error output.

        Returns dict with:
        - retry_after: seconds to wait (int or None)
        - error_message: the actual error message
        """
        info = {"retry_after": None, "error_message": stderr[:500]}

        # Try to extract retry-after seconds from error message
        # Common patterns: "retry after X seconds", "wait X seconds", "try again in X seconds"
        retry_patterns = [
            r"retry\s+after\s+(\d+)\s+seconds?",
            r"wait\s+(\d+)\s+seconds?",
            r"try\s+again\s+in\s+(\d+)\s+seconds?",
            r"retry-after:\s*(\d+)",
        ]

        for pattern in retry_patterns:
            match = re.search(pattern, stderr.lower())
            if match:
                info["retry_after"] = int(match.group(1))  # type: ignore[assignment]
                logger.info(f"Found retry-after: {info['retry_after']}s")
                break

        return info  # type: ignore[return-value]

    def calculate_wait_time(
        self, rate_limit_info: dict[str, int | str | None], task: Task
    ) -> int | None:
        """
        Calculate how long to wait before retrying based on retry-after header.

        Returns None if rate limit info cannot be extracted, indicating the task should fail.
        """
        if rate_limit_info["retry_after"] is not None:
            wait_seconds = rate_limit_info["retry_after"]
            logger.info(f"Using retry-after: {wait_seconds}s")
            return max(int(wait_seconds), 1)

        # No rate limit info available
        logger.error("Could not extract retry-after from rate limit error")
        return None

    # --- Morning report ---------------------------------------------------

    def write_morning_report(self, reason: str = "queue drained") -> None:
        """Write MORNING_REPORT.md summarising this worker run.

        Generated when the worker exits (queue drained or terminated) so the
        run can be reviewed at a glance: elapsed time, every task's final
        status/attempts/engine, the git commit hash on success or the failure
        summary otherwise.
        """
        tasks = self.queue.get_all_tasks()
        finished = datetime.now()
        started = self.started_at or finished
        elapsed = finished - started
        total_seconds = int(elapsed.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        counts = Counter(t.status for t in tasks)
        lines = [
            "# MORNING REPORT",
            "",
            f"- 生成时间: {finished.isoformat(timespec='seconds')}",
            f"- 开始时间: {started.isoformat(timespec='seconds')}",
            f"- 总耗时: {hours}h {minutes}m {seconds}s（{total_seconds}s）",
            f"- 退出原因: {reason}",
            f"- 引擎配置: primary={self.primary_engine}, fallback={self.fallback_engine}",
            f"- 任务统计: 共 {len(tasks)} 个"
            f"（completed={counts['completed']}, skipped={counts['skipped']}, "
            f"failed={counts['failed']}, rate_limited={counts['rate_limited']}, "
            f"queued={counts['queued']}, running={counts['running']}）",
            "",
            "| 任务 ID | 任务名称 | 最终状态 | 尝试次数 | 引擎 | Git Commit / 失败摘要 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]

        for task in tasks:
            if task.status == TaskStatus.COMPLETED.value and task.commit_hash:
                detail = f"`{task.commit_hash}`"
            elif task.status in (TaskStatus.SKIPPED.value, TaskStatus.FAILED.value):
                summary = (task.last_error or "").strip().replace("\n", " ").replace("|", "\\|")
                detail = f"{summary[:200]}" if summary else "—"
            else:
                detail = "—"

            name = (task.session_name or task.id).replace("|", "\\|")
            lines.append(
                f"| `{task.id}` | {name} | {task.status} | "
                f"{task.attempts}/{task.max_attempts} | {task.engine or '—'} | {detail} |"
            )

        lines.extend(
            [
                "",
                "---",
                "",
                "> 由 claude-queue worker 自动生成。"
                "状态 `skipped` 表示重试耗尽并已回滚，详情见工作区 `STUCK.md`。",
                "",
            ]
        )

        try:
            self.report_file.parent.mkdir(parents=True, exist_ok=True)
            self.report_file.write_text("\n".join(lines), encoding="utf-8")
            print(f"\n📋 Morning report written to: {self.report_file}")
        except Exception as e:
            logger.warning(f"Failed to write morning report: {e}")

    def _effective_prompt(self, task: Task) -> str:
        """Base prompt, plus verifier feedback accumulated from prior attempts."""
        if task.feedback and task.feedback.strip():
            return f"{task.prompt}\n\n{task.feedback}"
        return task.prompt

    def run_verify_commands(self, task: Task, cwd: Path | None) -> tuple[bool, str]:
        """Run the external hard verifier (`verify_commands`) in order.

        The task only counts as verified when *every* command exits 0. On the
        first failure the combined stdout/stderr tail (max 2000 chars) is
        returned so the worker can feed it back into the next attempt's prompt.
        """
        commands = task.verify_commands or []
        if not commands:
            return True, ""

        for command in commands:
            print(f"  [verify] $ {command}")
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=self.verify_timeout,
                    cwd=cwd,
                )
            except subprocess.TimeoutExpired:
                message = f"verify command timed out after {self.verify_timeout}s: {command}"
                print(f"  [verify] ✗ {message}")
                return False, message[-2000:]
            except Exception as e:
                message = f"verify command failed to start: {command} ({e})"
                print(f"  [verify] ✗ {message}")
                return False, message[-2000:]

            if result.returncode != 0:
                combined = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
                tail = combined[-2000:]
                print(f"  [verify] ✗ exit {result.returncode}: {tail[:200]}")
                return False, tail

            print("  [verify] ✓ passed")

        return True, ""

    # --- Git guard -------------------------------------------------------

    @staticmethod
    def _find_git_root(start: Path | None) -> Path | None:
        """Locate the enclosing git work tree by looking for a `.git` entry.

        Filesystem-only (no subprocess) so it is a cheap no-op outside a repo.
        """
        if start is None:
            return None
        current = start if start.is_dir() else start.parent
        for candidate in [current, *current.parents]:
            if (candidate / ".git").exists():
                return candidate
        return None

    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=120)

    def git_commit(self, task: Task, cwd: Path | None) -> None:
        """Archive a verified task: `git add .` then a tagged commit."""
        repo = self._find_git_root(cwd)
        if repo is None:
            logger.debug("No git repository detected; skipping auto-commit")
            return
        try:
            self._git(repo, "add", ".")
            commit = self._git(repo, "commit", "-m", f"AutoTask: [{task.id}] 完成")
            if commit.returncode == 0:
                head = self._git(repo, "rev-parse", "--short", "HEAD")
                if head.returncode == 0:
                    commit_hash = head.stdout.strip()
                    self.queue.update_task(task.id, commit_hash=commit_hash)
                    print(f"  [git] ✓ committed 'AutoTask: [{task.id}] 完成' ({commit_hash})")
                else:
                    print(f"  [git] ✓ committed 'AutoTask: [{task.id}] 完成'")
            else:
                detail = (commit.stdout or "") + (commit.stderr or "")
                print(f"  [git] commit skipped: {detail.strip()[:200]}")
        except Exception as e:
            logger.warning(f"Git auto-commit failed: {e}")

    def git_rollback(self, task: Task, cwd: Path | None) -> None:
        """Circuit-breaker: discard dirty changes left behind by a failed task."""
        repo = self._find_git_root(cwd)
        if repo is None:
            logger.debug("No git repository detected; skipping rollback")
            return
        try:
            self._git(repo, "reset", "--hard")
            self._git(repo, "clean", "-fd")
            print(f"  [git] ⟲ rolled back workspace for {task.id}")
        except Exception as e:
            logger.warning(f"Git rollback failed: {e}")

    def write_stuck(self, task: Task, cwd: Path | None, log: str) -> None:
        """Append a skipped-task report to STUCK.md inside the workspace."""
        base = cwd or Path.cwd()
        stuck_file = base / "STUCK.md"
        entry = (
            f"\n## {task.id} — {datetime.now().isoformat()}\n\n"
            f"- Status: SKIPPED (on_fail: skip)\n"
            f"- Session: {task.session_name}\n"
            f"- Attempts: {task.attempts}/{task.max_attempts}\n"
            f"- Engine (last): {task.engine or 'n/a'}\n"
            f"- Prompt: {task.prompt[:500]}\n\n"
            f"```\n{log.strip()}\n```\n"
        )
        try:
            with open(stuck_file, "a", encoding="utf-8") as f:
                f.write(entry)
            print(f"  [stuck] failure report appended to {stuck_file}")
        except Exception as e:
            logger.warning(f"Failed to write STUCK.md: {e}")

    # --- Failure handling ------------------------------------------------

    def _handle_failure(
        self,
        task: Task,
        attempt: int,
        cwd: Path | None,
        error: str,
        feedback: str = "",
    ) -> bool:
        """Central failure path: schedule a retry, or finish the task.

        While retries remain the task is re-queued and any verifier feedback is
        persisted so the next attempt (which runs on the fallback engine)
        receives the failure tail in its prompt. Once retries are exhausted the
        task follows `on_fail`:
          - "skip": git rollback circuit-breaker + STUCK.md + SKIPPED
          - "fail": mark FAILED (default)
        """
        error = (error or "").strip()
        exhausted = attempt >= task.max_attempts

        if not exhausted:
            print(
                f"↻ Task {task.id} re-queued for retry "
                f"({attempt}/{task.max_attempts}): {error[:200]}"
            )
            updates: dict = {
                "status": TaskStatus.QUEUED.value,
                "last_error": error[:500],
            }
            if feedback:
                existing = (task.feedback or "").strip()
                updates["feedback"] = f"{existing}\n\n{feedback}".strip() if existing else feedback
            self.queue.update_task(task.id, **updates)
            return False

        if (task.on_fail or "fail").lower() == "skip":
            print(f"⊘ Task {task.id} exhausted retries -> rollback + SKIPPED")
            if self.enable_git_guard:
                self.git_rollback(task, cwd)
            self.write_stuck(task, cwd, error)
            self.queue.update_task(
                task.id,
                status=TaskStatus.SKIPPED.value,
                last_error=error[:500],
            )
        else:
            print(f"✗ Task {task.id} failed: {error[:200]}")
            self.queue.update_task(task.id, status=TaskStatus.FAILED.value, last_error=error[:500])
        return False

    def execute_task(self, task: Task) -> bool:
        """Execute a single task. Returns True only if verified successful.

        Lifecycle:
          1. Pick the engine for this attempt (attempt 1 -> agy, >=2 -> codex).
          2. Run the CLI in the task workspace.
          3. On CLI success, run `verify_commands` (external hard verifier).
          4. All verifiers pass -> git auto-commit + COMPLETED.
          5. Otherwise feed the failure tail back into the prompt and retry;
             on exhaustion honour `on_fail` (skip -> rollback + STUCK.md).
        """
        attempt = task.attempts + 1

        print(f"\n{'=' * 60}")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Executing task: {task.id}")
        print(f"Session: {task.session_name}")
        if task.working_dir:
            print(f"Working dir: {task.working_dir}")
        print(f"Attempt: {attempt}/{task.max_attempts}")

        effective_prompt = self._effective_prompt(task)
        engine = select_engine(attempt, self.primary_engine, self.fallback_engine)
        binary = resolve_engine_binary(engine)

        if binary is None:
            alternative = (
                self.fallback_engine if engine == self.primary_engine else self.primary_engine
            )
            alt_binary = resolve_engine_binary(alternative)
            if alt_binary is not None:
                print(f"⚠ Engine '{engine}' not found on PATH; using '{alternative}' instead")
                engine, binary = alternative, alt_binary
            else:
                print(
                    f"⚠ Engine '{engine}' not found on PATH and no fallback available; "
                    f"attempting to run it anyway"
                )
                binary = ENGINE_BINARIES.get(engine, engine)

        print(f"Engine: {engine} ({binary})")
        preview = (
            effective_prompt[:100] + "..." if len(effective_prompt) > 100 else effective_prompt
        )
        print(f"Prompt: {preview}")
        print(f"{'=' * 60}\n")

        # Update task status
        self.queue.update_task(
            task.id,
            status=TaskStatus.RUNNING.value,
            started_at=datetime.now().isoformat(),
            attempts=attempt,
            engine=engine,
        )

        cwd = None
        if task.working_dir:
            cwd = Path(task.working_dir).expanduser().resolve()
            logger.info(f"Executing in directory: {cwd}")

        cmd = build_engine_command(engine, effective_prompt, binary)
        effective_timeout = task.timeout if task.timeout is not None else self.task_timeout

        try:
            # Execute the selected CLI engine.
            if self.stream_output:
                # Stream stdout to terminal; capture stderr for rate-limit detection
                result = subprocess.run(
                    cmd,
                    stdout=None,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=effective_timeout,
                    cwd=cwd,
                )
                stdout_out, stderr_out = "", result.stderr
            else:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=effective_timeout, cwd=cwd
                )
                stdout_out, stderr_out = result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            print(f"⏱ Task {task.id} timed out")
            return self._handle_failure(task, attempt, cwd, "Execution timeout")
        except Exception as e:
            print(f"✗ Task {task.id} error: {e}")
            return self._handle_failure(task, attempt, cwd, f"Execution error: {e}")

        if result.returncode != 0:
            # Check if rate limited
            if "rate limit" in stderr_out.lower() or "rate_limit" in stderr_out.lower():
                print(f"⏸ Task {task.id} hit rate limit")
                rate_limit_info = self.parse_rate_limit_info(stderr_out)
                self.queue.update_task(
                    task.id,
                    status=TaskStatus.RATE_LIMITED.value,
                    last_error=rate_limit_info["error_message"],
                )
                return False

            return self._handle_failure(
                task,
                attempt,
                cwd,
                stderr_out or f"CLI exited with code {result.returncode}",
            )

        # --- External hard verifier -------------------------------------
        verified, verify_log = self.run_verify_commands(task, cwd)
        if not verified:
            print(f"✗ Task {task.id} failed verification")
            feedback = "上次修改未通过验收，报错如下，请修复：\n" + verify_log
            return self._handle_failure(
                task,
                attempt,
                cwd,
                f"Verification failed:\n{verify_log}",
                feedback=feedback,
            )

        # --- Success: archive with git, then mark completed ---------------
        print(f"✓ Task {task.id} completed successfully")

        if self.enable_git_guard:
            self.git_commit(task, cwd)

        # Save output if requested
        if self.save_output:
            output_file = self.output_dir / f"{task.id}.txt"
            try:
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(f"Task: {task.id}\n")
                    f.write(f"Session: {task.session_name}\n")
                    f.write(f"Engine: {engine}\n")
                    f.write(f"Completed: {datetime.now().isoformat()}\n")
                    f.write("=" * 60 + "\n")
                    f.write(stdout_out)
                print(f"  Output saved to: {output_file}")
            except Exception as e:
                logger.warning(f"Failed to save output: {e}")

        self.queue.update_task(
            task.id,
            status=TaskStatus.COMPLETED.value,
            completed_at=datetime.now().isoformat(),
            feedback="",
        )
        return True

    def run(self):
        """Main worker loop"""
        self.started_at = datetime.now()
        print("Task Queue Worker Started")
        print(f"Engines: primary={self.primary_engine} fallback={self.fallback_engine}")
        print(f"Started at: {self.started_at.strftime('%Y-%m-%d %H:%M:%S')}")
        print("Press Ctrl+C to stop\n")

        # Reset tasks stuck in RUNNING from a previously interrupted worker
        running_tasks = [
            t for t in self.queue.get_all_tasks() if t.status == TaskStatus.RUNNING.value
        ]
        if running_tasks:
            print(f"⚠ Resetting {len(running_tasks)} interrupted task(s) to queued")
            for t in running_tasks:
                self.queue.update_task(t.id, status=TaskStatus.QUEUED.value)

        exit_reason = "queue drained"
        try:
            while self.running:
                task = self.queue.get_next_task()

                if task is None:
                    if self.idle:
                        print(
                            f"[{datetime.now().strftime('%H:%M:%S')}] No tasks. Polling again in {self.idle_interval}s..."
                        )
                        time.sleep(self.idle_interval)
                        continue
                    print(
                        f"\n[{datetime.now().strftime('%H:%M:%S')}] No tasks in queue. Worker exiting."
                    )
                    break

                success = self.execute_task(task)

                if not success:
                    # Check if task was rate limited and calculate wait time
                    updated_task = None
                    for t in self.queue.get_all_tasks():
                        if t.id == task.id:
                            updated_task = t
                            break

                    if updated_task and updated_task.status == TaskStatus.SKIPPED.value:
                        # Rolled back and parked; keep draining the rest of the queue
                        print(
                            f"\n⊘ Task {updated_task.id} skipped; continuing with the rest of the queue\n"
                        )
                        time.sleep(1)
                    elif updated_task and updated_task.status == TaskStatus.RATE_LIMITED.value:
                        # Parse rate limit info from last error
                        rate_limit_info = self.parse_rate_limit_info(updated_task.last_error or "")
                        delay = self.calculate_wait_time(rate_limit_info, updated_task)

                        if delay is None:
                            # Could not extract rate limit info - fail the task
                            print(
                                "\n✗ Could not extract retry-after from rate limit error. Marking task as failed.\n"
                            )
                            self.queue.update_task(
                                updated_task.id,
                                status=TaskStatus.FAILED.value,
                                last_error="Rate limited but could not extract retry-after information",
                            )
                        else:
                            # Wait for the specified time
                            print(f"\n⏳ Rate limited. Waiting {delay}s (from retry-after)...\n")
                            time.sleep(delay)
                    else:
                        # Non-rate-limit failure, use exponential backoff
                        delay = self.base_retry_delay * (2 ** min(task.attempts, 5))
                        print(f"\n⏳ Waiting {delay}s before next task...\n")
                        time.sleep(delay)
                else:
                    # Small delay between successful tasks
                    time.sleep(5)

        except KeyboardInterrupt:
            print("\n\n⚠ Worker stopped by user")
            self.running = False
            exit_reason = "terminated by user (Ctrl+C)"
        finally:
            # Always leave a run summary behind, on normal drain or termination.
            self.write_morning_report(reason=exit_reason)


def cmd_add(args, queue: TaskQueue):
    """Add new task to queue"""
    task = queue.add_task(
        prompt=args.prompt,
        session_name=args.session,
        max_attempts=args.max_attempts,
        priority=args.priority,
        working_dir=getattr(args, "working_dir", None),
        timeout=getattr(args, "timeout", None),
        verify_commands=getattr(args, "verify_command", None) or None,
        on_fail=getattr(args, "on_fail", "fail"),
    )
    print(f"✓ Task added: {task.id}")
    print(f"  Session: {task.session_name}")
    print(f"  Priority: {task.priority}")
    if task.working_dir:
        print(f"  Working dir: {task.working_dir}")
    if task.verify_commands:
        print(f"  Verify: {', '.join(task.verify_commands)}")
    print(f"  On fail: {task.on_fail}")
    print(
        f"  Prompt: {task.prompt[:80]}..." if len(task.prompt) > 80 else f"  Prompt: {task.prompt}"
    )


def cmd_worker(args, queue: TaskQueue):
    """Start worker process"""
    worker = ClaudeWorker(
        queue,
        base_retry_delay=args.retry_delay,
        save_output=args.save_output,
        stream_output=args.stream,
        idle=args.idle is not None,
        idle_interval=args.idle if args.idle is not None else 30,
        task_timeout=args.timeout,
        primary_engine=args.primary_engine,
        fallback_engine=args.fallback_engine,
        verify_timeout=args.verify_timeout,
        enable_git_guard=not args.no_git_guard,
        report_file=Path(args.report_file) if args.report_file else None,
    )
    worker.run()


def cmd_status(args, queue: TaskQueue):
    """Show queue status"""
    stats = queue.get_stats()

    print("\nTask Queue Status")
    print("=" * 40)
    print(f"Total tasks:      {stats['total']}")
    print(f"Queued:           {stats['queued']}")
    print(f"Running:          {stats['running']}")
    print(f"Completed:        {stats['completed']}")
    print(f"Failed:           {stats['failed']}")
    print(f"Rate Limited:     {stats['rate_limited']}")
    print(f"Skipped:          {stats['skipped']}")
    print("=" * 40)

    # Show next task
    next_task = queue.get_next_task()
    if next_task:
        print(f"\nNext task: {next_task.id}")
        print(f"  Session: {next_task.session_name}")
        print(f"  Priority: {next_task.priority}")
        print(f"  Attempts: {next_task.attempts}/{next_task.max_attempts}")


def cmd_list(args, queue: TaskQueue):
    """List all tasks"""
    tasks = queue.get_all_tasks()

    if not tasks:
        print("No tasks in queue")
        return

    # Filter by status if specified
    if args.status:
        tasks = [t for t in tasks if t.status == args.status]

    # Sort by priority and created_at
    tasks.sort(key=lambda t: (-t.priority, t.created_at))

    print(f"\nTotal tasks: {len(tasks)}\n")

    for task in tasks:
        status_icon = {
            TaskStatus.QUEUED.value: "⏳",
            TaskStatus.RUNNING.value: "▶️",
            TaskStatus.COMPLETED.value: "✓",
            TaskStatus.FAILED.value: "✗",
            TaskStatus.RATE_LIMITED.value: "⏸",
            TaskStatus.SKIPPED.value: "⊘",
        }.get(task.status, "?")

        print(f"{status_icon} {task.id}")
        print(
            f"   Status: {task.status} | Priority: {task.priority} | Attempts: {task.attempts}/{task.max_attempts}"
        )
        print(f"   Session: {task.session_name}")
        if task.working_dir:
            print(f"   Working dir: {task.working_dir}")
        if task.verify_commands:
            print(f"   Verify: {', '.join(task.verify_commands)}")
        if task.on_fail != "fail":
            print(f"   On fail: {task.on_fail}")
        if task.engine:
            print(f"   Engine (last): {task.engine}")
        print(f"   Created: {task.created_at}")

        prompt_preview = task.prompt[:100] + "..." if len(task.prompt) > 100 else task.prompt
        print(f"   Prompt: {prompt_preview}")

        if task.depends_on:
            deps_display = ", ".join(task.depends_on)
            print(f"   Dependencies: {deps_display}")

        if task.last_error:
            error_preview = (
                task.last_error[:80] + "..." if len(task.last_error) > 80 else task.last_error
            )
            print(f"   Last Error: {error_preview}")

        print()


def cmd_remove(args, queue: TaskQueue):
    """Remove task from queue"""
    queue.remove_task(args.task_id)
    print(f"✓ Task {args.task_id} removed")


def cmd_clear(args, queue: TaskQueue):
    """Clear completed tasks"""
    queue.clear_completed()
    print("✓ Completed tasks cleared")


def cmd_output(args, output_dir: Path):
    """Print saved output for a task"""
    output_file = output_dir / f"{args.task_id}.txt"
    if not output_file.exists():
        print(f"No output file for task {args.task_id}", file=sys.stderr)
        print(f"  Expected: {output_file}", file=sys.stderr)
        print("  (Run worker with --save-output to record outputs)", file=sys.stderr)
        sys.exit(1)
    with open(output_file) as f:
        print(f.read(), end="")


def load_batch_file(file_path: Path) -> list[dict]:
    """Load tasks from YAML or JSON file"""
    if not file_path.exists():
        raise QueueFileError(f"File not found: {file_path}")

    # Determine file type and load
    if file_path.suffix in [".yaml", ".yml"]:
        if not HAS_YAML:
            raise QueueError("PyYAML not installed. Install with: pip install pyyaml")
        try:
            with open(file_path) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise QueueFileError(f"Invalid YAML file: {e}") from e
    elif file_path.suffix == ".json":
        try:
            with open(file_path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise QueueFileError(f"Invalid JSON file: {e}") from e
    else:
        raise ValidationError(
            f"Unsupported file format: {file_path.suffix}. Use .yaml, .yml, or .json"
        )

    # Extract tasks
    if not isinstance(data, dict) or "tasks" not in data:
        raise ValidationError("Invalid file format. Expected a dictionary with 'tasks' key")

    tasks = data["tasks"]
    if not isinstance(tasks, list):
        raise ValidationError("'tasks' must be a list")

    # Validate each task has required fields
    for i, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            raise ValidationError(f"Task {i}: must be a dictionary")
        if "prompt" not in task:
            raise ValidationError(f"Task {i}: missing required field 'prompt'")
        verify_commands = task.get("verify_commands")
        if verify_commands is not None:
            if not isinstance(verify_commands, list) or not all(
                isinstance(c, str) and c.strip() for c in verify_commands
            ):
                raise ValidationError(
                    f"Task {i}: 'verify_commands' must be a list of non-empty strings"
                )
        if "on_fail" in task and task["on_fail"] not in ("fail", "skip"):
            raise ValidationError(f"Task {i}: 'on_fail' must be 'fail' or 'skip'")

    return tasks


def cmd_batch(args, queue: TaskQueue):
    """Load tasks from batch file with dependency resolution"""
    # Load tasks from file
    tasks = load_batch_file(args.file)

    logger.info(f"Loaded {len(tasks)} tasks from {args.file}")
    print(f"\nLoaded {len(tasks)} tasks from {args.file}")

    # Dry run - just show what would be added
    if args.dry_run:
        print("\nDRY RUN - Tasks that would be added:\n")
        for i, task_data in enumerate(tasks, 1):
            session = task_data.get("session", "auto-generated")
            priority = task_data.get("priority", 0)
            max_attempts = task_data.get("max_attempts", 3)
            depends_on = task_data.get("depends_on", [])
            verify_commands = task_data.get("verify_commands", [])
            on_fail = task_data.get("on_fail", "fail")
            prompt = task_data["prompt"]
            prompt_preview = prompt[:80] + "..." if len(prompt) > 80 else prompt

            print(f"{i}. Session: {session}")
            print(f"   Priority: {priority} | Max attempts: {max_attempts} | On fail: {on_fail}")
            if depends_on:
                print(f"   Depends on: {', '.join(depends_on)}")
            if verify_commands:
                print(f"   Verify: {', '.join(verify_commands)}")
            print(f"   Prompt: {prompt_preview}")
            print()
        return

    # Multi-pass approach for dependency resolution:
    # Keep adding tasks iteratively until all are added or no progress is made
    # This handles circular dependencies and complex dependency chains

    added_count = 0
    failed_count = 0
    session_to_id: dict[str, str] = {}  # Map session names to task IDs

    # Separate tasks by dependency status
    remaining_tasks = list(tasks)  # Copy of all tasks to process
    pass_number = 1

    while remaining_tasks:
        print("\n" + "=" * 60)
        print(f"PASS {pass_number}: Processing remaining tasks")
        print("=" * 60 + "\n")

        tasks_added_this_pass = 0
        still_remaining = []

        for task_data in remaining_tasks:
            try:
                # Resolve session names to task IDs for dependencies
                depends_on_sessions = task_data.get("depends_on", [])
                depends_on_ids = []

                if depends_on_sessions:
                    # Check if all dependencies can be resolved
                    can_resolve = True
                    for dep_session in depends_on_sessions:
                        if dep_session in session_to_id:
                            depends_on_ids.append(session_to_id[dep_session])
                        else:
                            # Dependency not yet added, skip this task for now
                            can_resolve = False
                            break

                    if not can_resolve:
                        # Dependencies not ready, try again next pass
                        still_remaining.append(task_data)
                        continue

                # Add the task
                task = queue.add_task(
                    prompt=task_data["prompt"],
                    session_name=task_data.get("session"),
                    priority=task_data.get("priority", 0),
                    max_attempts=task_data.get("max_attempts", 3),
                    depends_on=depends_on_ids if depends_on_ids else None,
                    working_dir=task_data.get("working_dir"),
                    timeout=task_data.get("timeout"),
                    verify_commands=task_data.get("verify_commands") or None,
                    on_fail=task_data.get("on_fail", "fail"),
                )
                session_display = task.session_name or "auto-generated"

                # Store mapping for dependency resolution
                if task_data.get("session"):
                    session_to_id[task_data["session"]] = task.id

                prompt_preview = task.prompt[:50] + "..." if len(task.prompt) > 50 else task.prompt
                dep_display = (
                    f" (depends on: {', '.join(depends_on_sessions)})"
                    if depends_on_sessions
                    else ""
                )
                print(f"✓ Added: {session_display} - {prompt_preview}{dep_display}")
                added_count += 1
                tasks_added_this_pass += 1

            except (ValidationError, QueueError) as e:
                # Task failed validation (e.g., circular dependency)
                session_display = task_data.get("session", "unnamed")
                print(f"✗ Failed: {session_display} - {e}")
                failed_count += 1

        # Check if we made progress
        if tasks_added_this_pass == 0 and still_remaining:
            # No progress made, remaining tasks have unresolvable dependencies
            print(
                f"\n⚠ No progress in pass {pass_number}. Remaining tasks have circular or missing dependencies:"
            )
            for task_data in still_remaining:
                session_display = task_data.get("session", "unnamed")
                deps = ", ".join(task_data.get("depends_on", []))
                print(f"✗ Failed: {session_display} - Cannot resolve dependencies: {deps}")
                failed_count += 1
            break

        remaining_tasks = still_remaining
        pass_number += 1

    # Summary
    print(f"\n{'=' * 60}")
    print(f"Added {added_count} tasks to queue")
    if failed_count > 0:
        print(f"Failed to add {failed_count} tasks")
    print(f"{'=' * 60}")

    # Show how to start worker
    if added_count > 0:
        print("\nStart worker with:")
        print("  ./claude-queue.py worker")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Task Queue Manager (dual-engine: agy / gemini / codex)\n\n"
            "For examples and documentation, see README_CN.md"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )

    # Queue file location
    default_queue = Path.home() / ".claude-queue" / "tasks.json"
    parser.add_argument(
        "--queue-file",
        type=Path,
        default=default_queue,
        help="Queue file location (default: ~/.claude-queue/tasks.json)",
    )

    subparsers = parser.add_subparsers(dest="command", required=False)

    # Add command
    add_parser = subparsers.add_parser("add", help="Add task to queue")
    add_parser.add_argument("prompt", help="Task prompt/description")
    add_parser.add_argument("--session", help="Session name (auto-generated if not provided)")
    add_parser.add_argument(
        "--max-attempts", type=int, default=3, help="Max retry attempts (default: 3)"
    )
    add_parser.add_argument(
        "--priority", type=int, default=0, help="Task priority (higher = first, default: 0)"
    )
    add_parser.add_argument(
        "--working-dir", help="Working directory for task execution (default: current directory)"
    )
    add_parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Task execution timeout in seconds, overrides global --timeout (default: 3600)",
    )
    add_parser.add_argument(
        "--verify-command",
        action="append",
        default=None,
        metavar="CMD",
        help="Verification command that must exit 0 (repeatable, e.g. --verify-command 'pytest')",
    )
    add_parser.add_argument(
        "--on-fail",
        choices=["fail", "skip"],
        default="fail",
        help="Policy when retries are exhausted: 'fail' or 'skip' (rollback + STUCK.md)",
    )

    # Worker command
    worker_parser = subparsers.add_parser("worker", help="Start task worker")
    worker_parser.add_argument(
        "--retry-delay", type=int, default=60, help="Base retry delay in seconds (default: 60)"
    )
    worker_parser.add_argument(
        "--primary-engine",
        default=DEFAULT_PRIMARY_ENGINE,
        choices=sorted(ENGINE_BINARIES),
        help=f"Engine used on the first attempt (default: {DEFAULT_PRIMARY_ENGINE})",
    )
    worker_parser.add_argument(
        "--fallback-engine",
        default=DEFAULT_FALLBACK_ENGINE,
        choices=sorted(ENGINE_BINARIES),
        help=f"Engine used on retries (default: {DEFAULT_FALLBACK_ENGINE})",
    )
    worker_parser.add_argument(
        "--verify-timeout",
        type=int,
        default=600,
        help="Timeout per verify command in seconds (default: 600)",
    )
    worker_parser.add_argument(
        "--no-git-guard",
        action="store_true",
        help="Disable git auto-commit on success and git rollback on skip",
    )
    worker_parser.add_argument(
        "--save-output", action="store_true", help="Save task outputs to ~/.claude-queue/outputs/"
    )
    worker_parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream engine output to terminal in real-time (disables output saving)",
    )
    worker_parser.add_argument(
        "--report-file",
        default=None,
        metavar="PATH",
        help="Path for MORNING_REPORT.md (default: ./MORNING_REPORT.md)",
    )
    worker_parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Default task execution timeout in seconds (default: 3600)",
    )
    worker_parser.add_argument(
        "--idle",
        nargs="?",
        const=30,
        default=None,
        type=int,
        metavar="SECONDS",
        help="Keep worker running when queue is empty, polling every N seconds (default: 30)",
    )

    # Status command
    subparsers.add_parser("status", help="Show queue status")

    # List command
    list_parser = subparsers.add_parser("list", help="List all tasks")
    list_parser.add_argument(
        "--status", choices=[s.value for s in TaskStatus], help="Filter by status"
    )

    # Remove command
    remove_parser = subparsers.add_parser("remove", help="Remove task from queue")
    remove_parser.add_argument("task_id", help="Task ID to remove")

    # Clear command
    subparsers.add_parser("clear", help="Clear completed tasks")

    # Output command
    output_parser = subparsers.add_parser("output", help="Print saved output for a task")
    output_parser.add_argument("task_id", help="Task ID")

    # Batch command
    batch_parser = subparsers.add_parser("batch", help="Load tasks from YAML/JSON file")
    batch_parser.add_argument("file", type=Path, help="Task file (YAML or JSON)")
    batch_parser.add_argument(
        "--dry-run", action="store_true", help="Show tasks without adding them"
    )

    args = parser.parse_args()

    # Show help if no command provided
    if not args.command:
        parser.print_help()
        sys.exit(0)

    output_dir = Path.home() / ".claude-queue" / "outputs"

    # Initialize queue (not needed for the output command)
    queue = TaskQueue(args.queue_file) if args.command != "output" else None

    # Execute command
    if args.command == "add":
        cmd_add(args, queue)
    elif args.command == "worker":
        cmd_worker(args, queue)
    elif args.command == "status":
        cmd_status(args, queue)
    elif args.command == "list":
        cmd_list(args, queue)
    elif args.command == "remove":
        cmd_remove(args, queue)
    elif args.command == "clear":
        cmd_clear(args, queue)
    elif args.command == "output":
        cmd_output(args, output_dir)
    elif args.command == "batch":
        cmd_batch(args, queue)


if __name__ == "__main__":
    try:
        main()
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except QueueFileError as e:
        logger.error(f"Queue file error: {e}")
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    except QueueError as e:
        logger.error(f"Queue error: {e}")
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(3)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.exception("Unexpected error")
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)
