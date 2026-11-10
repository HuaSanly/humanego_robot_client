#!/usr/bin/env python3
"""Local-only process supervisor and web API for the teleoperation stack."""

from __future__ import annotations

import argparse
import collections
import json
import math
import mimetypes
import os
import pwd
import queue
import re
import secrets
import signal
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
import xmlrpc.client
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
CONFIG_PATH = APP_DIR / "tasks.json"
PID_PATH = APP_DIR / ".controller.pid"
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
MAX_LOG_LINES = 1200
LOG_BATCH_INTERVAL_SEC = 0.10


def now_text() -> str:
    return time.strftime("%H:%M:%S")


def clean_line(value: str) -> str:
    value = ANSI_RE.sub("", value.rstrip("\r\n"))
    return "".join(ch for ch in value if ch == "\t" or ord(ch) >= 32)


def matching_controller_pids() -> set[int]:
    """Find older controllers launched from this exact installation path."""
    server_path = os.fsencode(str(Path(__file__).resolve()))
    matches: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        if server_path in arguments:
            matches.add(pid)
    return matches


def replace_previous_controller() -> None:
    previous = matching_controller_pids()
    if not previous:
        return
    print(f"发现旧控制服务 {sorted(previous)}，正在安全接管…", flush=True)
    for pid in previous:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 6.0
    remaining = set(previous)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if Path(f"/proc/{pid}").exists()}
        if remaining:
            time.sleep(0.1)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if remaining:
        print(f"旧控制服务未及时退出，已强制结束 {sorted(remaining)}", flush=True)
        time.sleep(0.25)


def write_pid_file() -> None:
    PID_PATH.write_text(f"{os.getpid()}\n", encoding="ascii")
    PID_PATH.chmod(0o600)


def remove_pid_file() -> None:
    try:
        recorded = PID_PATH.read_text(encoding="ascii").strip()
        if recorded == str(os.getpid()):
            PID_PATH.unlink()
    except (FileNotFoundError, PermissionError, OSError):
        pass


@dataclass
class RuntimeTask:
    spec: dict[str, Any]
    status: str = "stopped"
    process: subprocess.Popen[str] | None = None
    started_at: float | None = None
    stopped_at: float | None = None
    exit_code: int | None = None
    control_mode: str | None = None
    generation: int = 0
    log_seq: int = 0
    logs: collections.deque[dict[str, Any]] = field(
        default_factory=lambda: collections.deque(maxlen=MAX_LOG_LINES)
    )
    lock: threading.RLock = field(default_factory=threading.RLock)
    action_lock: threading.RLock = field(default_factory=threading.RLock)


class TeleopManager:
    def __init__(self, config: dict[str, Any], operator_user: str, operator_home: str):
        self.config = config
        self.operator_user = operator_user
        self.operator_home = operator_home
        self.operator_uid = pwd.getpwnam(operator_user).pw_uid
        self.tasks = {
            item["id"]: RuntimeTask(item)
            for item in sorted(config["tasks"], key=lambda x: x["order"])
        }
        self.subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self.subscribers_lock = threading.Lock()
        self.pending_logs: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        self.pending_logs_lock = threading.Lock()
        self.operation_lock = threading.Lock()
        self.stop_request_lock = threading.Lock()
        self.cancel_sequence = threading.Event()
        self.stop_requested = threading.Event()
        self.instance_id = secrets.token_hex(8)
        self.sequence = {
            "status": "idle",
            "current": None,
            "message": "等待操作",
        }
        self.shutting_down = threading.Event()
        threading.Thread(
            target=self._flush_log_events,
            daemon=True,
            name="log-event-batcher",
        ).start()

    def emit(self, event: dict[str, Any]) -> None:
        event["at"] = time.time()
        event["instance_id"] = self.instance_id
        stale: list[queue.Queue[dict[str, Any]]] = []
        with self.subscribers_lock:
            for target in self.subscribers:
                try:
                    target.put_nowait(event)
                except queue.Full:
                    stale.append(target)
            for target in stale:
                self.subscribers.discard(target)

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        target: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2000)
        with self.subscribers_lock:
            self.subscribers.add(target)
        return target

    def unsubscribe(self, target: queue.Queue[dict[str, Any]]) -> None:
        with self.subscribers_lock:
            self.subscribers.discard(target)

    def add_log(self, task: RuntimeTask, text: str, level: str = "output") -> None:
        line = clean_line(text)
        if not line and level == "output":
            return
        with task.lock:
            task.log_seq += 1
            item = {
                "seq": task.log_seq,
                "time": now_text(),
                "level": level,
                "text": line,
            }
            task.logs.append(item)
        with self.pending_logs_lock:
            self.pending_logs[task.spec["id"]].append(item)

    def _flush_log_events(self) -> None:
        while not self.shutting_down.wait(LOG_BATCH_INTERVAL_SEC):
            self._flush_pending_logs()
        self._flush_pending_logs()

    def _flush_pending_logs(self) -> None:
        with self.pending_logs_lock:
            if not self.pending_logs:
                return
            batches = self.pending_logs
            self.pending_logs = collections.defaultdict(list)
        for task_id, items in batches.items():
            self.emit({"type": "logs", "task_id": task_id, "items": items})

    def set_status(self, task: RuntimeTask, status: str, exit_code: int | None = None) -> None:
        with task.lock:
            task.status = status
            task.exit_code = exit_code
        self.emit({"type": "task", "task": self.task_snapshot(task, include_logs=False)})

    def task_snapshot(self, task: RuntimeTask, include_logs: bool = True) -> dict[str, Any]:
        with task.lock:
            process = task.process
            pid = process.pid if process is not None and process.poll() is None else None
            result = {
                "id": task.spec["id"],
                "order": task.spec["order"],
                "title": task.spec["title"],
                "subtitle": task.spec["subtitle"],
                "group": task.spec["group"],
                "accent": task.spec.get("accent", "cyan"),
                "privileged": task.spec.get("run_as") == "root",
                "status": task.status,
                "pid": pid,
                "started_at": task.started_at,
                "stopped_at": task.stopped_at,
                "exit_code": task.exit_code,
                "control_mode": task.control_mode or task.spec.get("default_control_mode"),
                "control_modes": task.spec.get("control_modes", []),
            }
            if include_logs:
                result["logs"] = list(task.logs)
            return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "operator": self.operator_user,
            "sequence": dict(self.sequence),
            "tasks": [self.task_snapshot(task) for task in self.tasks.values()],
            "server_time": time.time(),
        }

    def update_sequence(self, status: str, current: str | None, message: str) -> None:
        self.sequence = {"status": status, "current": current, "message": message}
        self.emit({"type": "sequence", "sequence": dict(self.sequence)})

    def _read_proc_table(self) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", "replace"
                )
                status_text = (entry / "status").read_text(encoding="utf-8", errors="replace")
                ppid_match = re.search(r"^PPid:\s+(\d+)", status_text, re.MULTILINE)
                uid_match = re.search(r"^Uid:\s+(\d+)", status_text, re.MULTILINE)
                try:
                    environ = (entry / "environ").read_bytes().split(b"\0")
                except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
                    environ = []
                result[pid] = {
                    "cmdline": cmdline.strip(),
                    "ppid": int(ppid_match.group(1)) if ppid_match else 0,
                    "uid": int(uid_match.group(1)) if uid_match else -1,
                    "environ": environ,
                }
            except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
                continue
        return result

    @staticmethod
    def _descendants(table: dict[int, dict[str, Any]], roots: set[int]) -> set[int]:
        found = set(roots)
        changed = True
        while changed:
            changed = False
            for pid, info in table.items():
                if info["ppid"] in found and pid not in found:
                    found.add(pid)
                    changed = True
        return found

    def _find_conflicting_processes(self, task: RuntimeTask) -> list[tuple[int, str]]:
        """返回与任务声明的互斥进程特征匹配的外部进程。"""
        patterns = [str(item) for item in task.spec.get("conflict_process_patterns", [])]
        if not patterns:
            return []

        table = self._read_proc_table()
        permitted_uids = {0, self.operator_uid}
        own_pid = os.getpid()
        matches = {
            pid: info["cmdline"]
            for pid, info in table.items()
            if pid != own_pid
            and info["uid"] in permitted_uids
            and info["cmdline"]
            and any(pattern in info["cmdline"] for pattern in patterns)
        }

        # 同一模型部署会同时命中顶层 roslaunch 与子节点；仅报告最上层命中项，
        # 使操作者向该 PID 发送 SIGINT 即可让 roslaunch 正常回收其子节点。
        roots: list[tuple[int, str]] = []
        for pid, command in matches.items():
            parent = table[pid]["ppid"]
            has_matching_ancestor = False
            while parent in table:
                if parent in matches:
                    has_matching_ancestor = True
                    break
                parent = table[parent]["ppid"]
            if not has_matching_ancestor:
                roots.append((pid, command))
        return sorted(roots)

    @staticmethod
    def _ros_topic_publishers(topic: str) -> tuple[list[str] | None, str]:
        """从 ROS master 查询指定话题的发布者；失败时返回原因。"""
        master_uri = os.environ.get("ROS_MASTER_URI", "http://localhost:11311")
        try:
            master = xmlrpc.client.ServerProxy(master_uri, allow_none=True)
            code, message, system_state = master.getSystemState("/teleop_control_center")
            if code != 1:
                return None, f"ROS master 返回错误：{message}"

            publishers = system_state[0]
            for topic_name, nodes in publishers:
                if topic_name == topic:
                    return sorted(set(str(node) for node in nodes)), ""
            return [], ""
        except Exception as exc:
            return None, f"无法连接 ROS master ({master_uri})：{exc}"

    def _check_start_guards(self, task: RuntimeTask) -> bool:
        """执行任务的非破坏性启动前安全检查。"""
        conflicts = self._find_conflicting_processes(task)
        if conflicts:
            details = "; ".join(f"PID {pid}: {command[:180]}" for pid, command in conflicts)
            self.add_log(task, "检测到与当前任务互斥的外部命令源，已拒绝启动", "error")
            self.add_log(task, f"请先在原终端正常停止以下进程：{details}", "error")
            return False

        topic = str(task.spec.get("exclusive_publisher_topic", "")).strip()
        if not topic:
            return True

        publishers, error = self._ros_topic_publishers(topic)
        if publishers is None:
            self.add_log(task, f"无法验证 {topic} 的发布者独占性，已拒绝启动：{error}", "error")
            return False
        if publishers:
            self.add_log(task, f"检测到 {topic} 已有发布者，已拒绝启动：{', '.join(publishers)}", "error")
            self.add_log(task, "请先停止外部控制节点，确认该话题没有发布者后再启动 IK。", "error")
            return False

        self.add_log(task, f"命令源互锁检查通过：{topic} 当前没有发布者", "system")
        return True

    def cleanup_residual(self, task: RuntimeTask) -> int:
        with task.lock:
            if (
                task.process is not None
                and task.process.poll() is None
                and task.status != "stopping"
            ):
                self.add_log(task, "任务正在运行，已跳过残留扫描", "system")
                return -1
        patterns = [str(item) for item in task.spec.get("cleanup_patterns", [])]

        table = self._read_proc_table()
        own_pid = os.getpid()
        permitted_uids = {0, self.operator_uid}
        task_marker = f"TELEOP_TASK_ID={task.spec['id']}".encode()
        matches = {
            pid
            for pid, info in table.items()
            if pid != own_pid
            and info["uid"] in permitted_uids
            and info["cmdline"]
            and (
                task_marker in info["environ"]
                or any(pattern in info["cmdline"] for pattern in patterns)
            )
        }
        targets = self._descendants(table, matches)
        targets.discard(own_pid)
        if not targets:
            self.add_log(task, "残留进程检查通过", "system")
            return 0

        details = ", ".join(f"{pid}:{table.get(pid, {}).get('cmdline', '')[:80]}" for pid in sorted(matches))
        self.add_log(task, f"发现残留进程，准备清理 → {details}", "warning")
        self.add_log(task, "先发送 Ctrl+C 信号，等待驱动执行失能回调", "system")
        self._signal_pids(targets, signal.SIGINT, table)
        deadline = time.monotonic() + 2.5
        remaining = set(targets)
        while remaining and time.monotonic() < deadline:
            remaining = {pid for pid in remaining if Path(f"/proc/{pid}").exists()}
            if remaining:
                time.sleep(0.1)
        if remaining:
            self.add_log(task, f"{len(remaining)} 个进程未响应 Ctrl+C，发送 TERM", "warning")
            self._signal_pids(remaining, signal.SIGTERM, table)
            deadline = time.monotonic() + 2.0
            while remaining and time.monotonic() < deadline:
                remaining = {pid for pid in remaining if Path(f"/proc/{pid}").exists()}
                if remaining:
                    time.sleep(0.1)
        if remaining:
            self._signal_pids(remaining, signal.SIGKILL, table)
            self.add_log(task, f"强制结束 {len(remaining)} 个未退出进程", "warning")
        self.add_log(task, f"已清理 {len(targets)} 个残留进程", "system")
        return len(targets)

    @staticmethod
    def _signal_pids(
        pids: set[int], sig: signal.Signals, table: dict[int, dict[str, Any]] | None = None
    ) -> None:
        def depth(pid: int) -> int:
            if table is None:
                return 0
            result = 0
            seen: set[int] = set()
            current = pid
            while current in table and current not in seen:
                seen.add(current)
                parent = table[current]["ppid"]
                if parent not in pids:
                    break
                result += 1
                current = parent
            return result

        # Send the signal to children before wrappers so cleanup callbacks can finish.
        for pid in sorted(pids, key=lambda item: (depth(item), item), reverse=True):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def _command_for(
        self,
        task: RuntimeTask,
        control_mode: str | None = None,
        command: str | None = None,
    ) -> list[str]:
        command = command or task.spec["command"]
        if task.spec.get("control_modes"):
            mode = control_mode or task.spec["default_control_mode"]
            command += f" --control-mode {shlex.quote(mode)}"
        shell_flag = "-ic" if task.spec.get("shell_mode") == "interactive" else "-lc"
        if task.spec.get("run_as") == "root":
            return ["/bin/bash", shell_flag, command]
        return [
            "/usr/bin/sudo",
            "-H",
            "-u",
            self.operator_user,
            "/usr/bin/env",
            f"TELEOP_HOME={self.operator_home}",
            f"TELEOP_TASK_ID={task.spec['id']}",
            f"TELEOP_CONSOLE_DIR={APP_DIR}",
            "/bin/bash",
            shell_flag,
            command,
        ]

    def start_task(
        self, task_id: str, cleanup: bool = True, control_mode: str | None = None
    ) -> dict[str, Any]:
        task = self.tasks[task_id]
        with task.action_lock:
            return self._start_task(task, cleanup, control_mode)

    def _start_task(
        self, task: RuntimeTask, cleanup: bool, control_mode: str | None = None
    ) -> dict[str, Any]:
        allowed_modes = task.spec.get("control_modes", [])
        if allowed_modes:
            control_mode = control_mode or task.control_mode or task.spec.get("default_control_mode")
            if control_mode not in allowed_modes:
                self.add_log(task, f"不支持的客户端控制模式：{control_mode}", "error")
                self.set_status(task, "error", 2)
                return self.task_snapshot(task)
        with task.lock:
            if task.process is not None and task.process.poll() is None:
                self.add_log(task, "任务已在运行，本次启动已忽略", "system")
                return self.task_snapshot(task)
            task.generation += 1
            generation = task.generation
            task.started_at = time.time()
            task.stopped_at = None
            task.exit_code = None
            task.control_mode = control_mode

        self.set_status(task, "cleaning" if cleanup else "starting")
        self.add_log(task, "──────── 启动任务 ────────", "system")
        if cleanup:
            self.cleanup_residual(task)
        if not self._check_start_guards(task):
            with task.lock:
                task.stopped_at = time.time()
                task.exit_code = 2
            self.set_status(task, "error", 2)
            return self.task_snapshot(task)
        self.set_status(task, "starting")

        env = os.environ.copy()
        env["TELEOP_HOME"] = self.operator_home
        env["TELEOP_TASK_ID"] = task.spec["id"]
        env["TELEOP_CONSOLE_DIR"] = str(APP_DIR)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            process = subprocess.Popen(
                self._command_for(task, control_mode),
                cwd=self.operator_home,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except Exception as exc:
            self.add_log(task, f"启动失败：{exc}", "error")
            self.set_status(task, "error", 127)
            return self.task_snapshot(task)

        with task.lock:
            task.process = process
        self.add_log(task, f"进程已启动 · PID {process.pid}", "system")
        needs_readiness = bool(task.spec.get("readiness_command"))
        self.set_status(task, "starting" if needs_readiness else "running")
        threading.Thread(
            target=self._capture_output,
            args=(task, process, generation),
            daemon=True,
            name=f"log-{task.spec['id']}",
        ).start()
        threading.Thread(
            target=self._watch_process,
            args=(task, process, generation),
            daemon=True,
            name=f"watch-{task.spec['id']}",
        ).start()
        if needs_readiness:
            self.add_log(task, "进程已创建，正在等待电机反馈；检查通过后才继续启动", "system")
            threading.Thread(
                target=self._check_readiness,
                args=(task, process, generation, env),
                daemon=True,
                name=f"ready-{task.spec['id']}",
            ).start()
        return self.task_snapshot(task)

    def _check_readiness(
        self,
        task: RuntimeTask,
        process: subprocess.Popen[str],
        generation: int,
        env: dict[str, str],
    ) -> None:
        probe: subprocess.Popen[str] | None = None
        error = ""
        output = ""
        try:
            timeout = float(task.spec.get("startup_timeout_sec", 20.0))
            deadline = time.monotonic() + timeout
            probe = subprocess.Popen(
                self._command_for(task, command=task.spec["readiness_command"]),
                cwd=self.operator_home,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            while True:
                with task.lock:
                    if (
                        task.generation != generation
                        or task.process is not process
                        or task.status != "starting"
                        or process.poll() is not None
                        or self.shutting_down.is_set()
                    ):
                        return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = f"启动检查超过 {timeout:g}s，未确认电机反馈"
                    break
                try:
                    output, _ = probe.communicate(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                if probe.returncode != 0:
                    error = f"电机反馈检查失败（检查进程退出码 {probe.returncode}）"
                break
        except Exception as exc:
            error = f"无法执行启动检查：{exc}"
        finally:
            if probe is not None:
                if probe.poll() is None:
                    try:
                        probe.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        probe.communicate(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        try:
                            probe.kill()
                        except ProcessLookupError:
                            pass
                        probe.communicate()
                else:
                    probe.communicate()

        with task.lock:
            if (
                task.generation != generation
                or task.process is not process
                or task.status != "starting"
                or process.poll() is not None
            ):
                return
            for line in output.splitlines():
                self.add_log(task, line)
            if error:
                self.add_log(task, error, "error")
                self.add_log(task, "下游任务已暂停；驱动仍由控制台管理，可使用停止按钮处理", "error")
                self.set_status(task, "error")
            else:
                self.add_log(task, "已收到电机反馈，启动检查通过", "system")
                self.set_status(task, "running")

    def _capture_output(
        self, task: RuntimeTask, process: subprocess.Popen[str], generation: int
    ) -> None:
        if process.stdout is None:
            return
        try:
            for line in iter(process.stdout.readline, ""):
                with task.lock:
                    if task.generation != generation:
                        break
                self.add_log(task, line)
        finally:
            try:
                process.stdout.close()
            except OSError:
                pass

    def _watch_process(
        self, task: RuntimeTask, process: subprocess.Popen[str], generation: int
    ) -> None:
        code = process.wait()
        with task.lock:
            if task.generation != generation:
                return
            previous = task.status
            task.stopped_at = time.time()
            task.exit_code = code
            task.process = None
        if previous in ("stopping", "stopped"):
            self.add_log(task, "主进程已退出，正在确认子进程退出与电机失能", "system")
        elif previous in ("starting", "error") and task.spec.get("readiness_command"):
            self.add_log(task, "驱动在电机反馈检查通过前退出", "error")
            self.set_status(task, "error", code)
        elif code == 0:
            self.add_log(task, "命令已正常执行完成", "system")
            self.set_status(task, "finished", code)
        else:
            self.add_log(task, f"进程异常退出 · code {code}", "error")
            self.set_status(task, "error", code)

    def stop_task(self, task_id: str, cleanup: bool = True) -> dict[str, Any]:
        task = self.tasks[task_id]
        with task.action_lock:
            return self._stop_task(task, cleanup)

    def _stop_task(self, task: RuntimeTask, cleanup: bool) -> dict[str, Any]:
        with task.lock:
            process = task.process
            if process is not None and process.poll() is None:
                self.set_status(task, "stopping")
                self.add_log(task, "发送 Ctrl+C，等待驱动执行安全失能…", "system")
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except (ProcessLookupError, PermissionError):
                    try:
                        process.send_signal(signal.SIGINT)
                    except ProcessLookupError:
                        pass

        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=float(task.spec.get("stop_grace_sec", 3.5)))
            except subprocess.TimeoutExpired:
                self.add_log(task, "Ctrl+C 清理超时，发送 TERM", "warning")
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.add_log(task, "TERM 清理超时，执行最终 KILL", "warning")
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    try:
                        process.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        self.add_log(task, "外层进程未能确认退出，将继续扫描任务残留", "warning")
        if process is not None and process.poll() is not None:
            with task.lock:
                task.exit_code = process.returncode
        if cleanup:
            clean_rounds = 0
            for _attempt in range(4):
                removed = self.cleanup_residual(task)
                if removed < 0:
                    clean_rounds = 0
                elif removed == 0:
                    clean_rounds += 1
                    if clean_rounds >= 2:
                        break
                else:
                    clean_rounds = 0
                time.sleep(0.35)
        settle_time = float(task.spec.get("stop_settle_sec", 0.0))
        if settle_time > 0:
            self.add_log(task, f"等待 {settle_time:.1f}s 确认电机失能稳定", "system")
            time.sleep(settle_time)
        with task.lock:
            if task.process is None or task.process.poll() is not None:
                task.process = None
                task.stopped_at = time.time()
                self.set_status(task, "stopped", task.exit_code)
                self.add_log(task, "停止完成：主进程、子进程与残留均已处理", "system")
            else:
                self.add_log(task, "停止未完成：仍检测到外层进程", "error")
                self.set_status(task, "error", task.process.poll())
        return self.task_snapshot(task)

    def restart_task(self, task_id: str, control_mode: str | None = None) -> dict[str, Any]:
        task = self.tasks[task_id]
        with task.action_lock:
            self.stop_task(task_id, cleanup=True)
            return self.start_task(task_id, cleanup=True, control_mode=control_mode)

    def clear_logs(self, task_id: str) -> dict[str, Any]:
        task = self.tasks[task_id]
        with task.lock:
            task.logs.clear()
        self.emit({"type": "logs_cleared", "task_id": task_id})
        return self.task_snapshot(task)

    def cleanup_all(self) -> None:
        for task in reversed(list(self.tasks.values())):
            self.cleanup_residual(task)

    def cancel_start_sequence(self) -> None:
        self.cancel_sequence.set()

    def _sequence_was_cancelled(self) -> bool:
        if not self.cancel_sequence.is_set():
            return False
        self.update_sequence("stopping", None, "启动序列已取消，等待执行安全停止")
        return True

    def run_sequence(self, restart: bool = False) -> None:
        if not self.operation_lock.acquire(blocking=False):
            return
        try:
            if restart:
                self.update_sequence("stopping", None, "正在按逆序停止全部任务")
                for task_id in reversed(list(self.tasks.keys())):
                    self.stop_task(task_id, cleanup=True)
                    if self._sequence_was_cancelled():
                        return

            self.update_sequence("running", None, "正在按依赖顺序启动")
            startup_tasks = [
                (task_id, task)
                for task_id, task in self.tasks.items()
                if task.spec.get("auto_start", True)
            ]
            for task_id, task in startup_tasks:
                if self.shutting_down.is_set() or self._sequence_was_cancelled():
                    return
                self.update_sequence("running", task_id, f"正在启动：{task.spec['title']}")
                with task.lock:
                    is_running = task.process is not None and task.process.poll() is None
                if not is_running:
                    self.start_task(task_id, cleanup=True)

                needs_readiness = bool(task.spec.get("readiness_command"))
                wait_for_exit = task.spec.get("wait_for_exit", False)
                needs_confirmation = needs_readiness or wait_for_exit
                delay = float(task.spec.get(
                    "startup_timeout_sec" if needs_confirmation else "startup_delay_sec",
                    20.0 if needs_confirmation else 1.5,
                ))
                deadline = time.monotonic() + delay
                while True:
                    if self._sequence_was_cancelled():
                        return
                    with task.lock:
                        status = task.status
                    failed = status in ("error", "stopped", "stopping")
                    if needs_readiness and status == "finished":
                        failed = True
                    if failed:
                        self.update_sequence(
                            "failed", task_id, f"{task.spec['title']} 启动失败，序列已暂停"
                        )
                        return
                    if needs_readiness and status == "running":
                        break
                    if wait_for_exit and status == "finished":
                        break
                    if time.monotonic() >= deadline:
                        if needs_confirmation:
                            self.update_sequence("failed", task_id, f"{task.spec['title']} 启动检查超时，序列已暂停")
                            return
                        break
                    time.sleep(0.1)
            self.update_sequence("done", None, "全部任务启动序列已完成")
        except Exception as exc:
            self.update_sequence("failed", None, f"启动序列异常：{exc}")
        finally:
            self.operation_lock.release()

    def start_sequence_async(self, restart: bool = False) -> bool:
        if self.operation_lock.locked() or self.stop_requested.is_set():
            return False
        self.cancel_sequence.clear()
        threading.Thread(
            target=self.run_sequence,
            args=(restart,),
            daemon=True,
            name="start-sequence",
        ).start()
        return True

    def stop_all(self) -> None:
        with self.stop_request_lock:
            self.cancel_sequence.set()
            with self.operation_lock:
                try:
                    self.update_sequence("stopping", None, "正在按逆序安全失能并停止全部任务")
                    for task_id in reversed(list(self.tasks.keys())):
                        self.stop_task(task_id, cleanup=True)
                    self.update_sequence("idle", None, "全部任务已停止，电机驱动已执行失能清理")
                finally:
                    self.stop_requested.clear()

    def stop_all_async(self) -> bool:
        if self.stop_requested.is_set():
            return True
        self.stop_requested.set()
        self.cancel_sequence.set()
        threading.Thread(target=self.stop_all, daemon=True, name="stop-all").start()
        return True

    def shutdown(self) -> None:
        if self.shutting_down.is_set():
            return
        self.shutting_down.set()
        self.cancel_sequence.set()
        self.update_sequence("stopping", None, "控制台退出，正在停止全部任务")
        for task_id in reversed(list(self.tasks.keys())):
            try:
                self.stop_task(task_id, cleanup=True)
            except Exception:
                pass


class TeleopHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], manager: TeleopManager, token: str):
        super().__init__(address, RequestHandler)
        self.manager = manager
        self.token = token


class RequestHandler(BaseHTTPRequestHandler):
    server: TeleopHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string: str, *args: object) -> None:
        if self.path.startswith("/api/events"):
            return
        sys.stdout.write(f"[web] {self.address_string()} {format_string % args}\n")

    def _parsed(self) -> urllib.parse.ParseResult:
        return urllib.parse.urlparse(self.path)

    def _authorized(self) -> bool:
        parsed = self._parsed()
        query_token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        header_token = self.headers.get("X-Teleop-Token", "")
        return secrets.compare_digest(query_token or header_token, self.server.token)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "控制令牌无效"})
        return False

    def do_GET(self) -> None:
        parsed = self._parsed()
        if parsed.path == "/api/state":
            if self._require_auth():
                self._json(HTTPStatus.OK, self.server.manager.snapshot())
            return
        if parsed.path == "/api/events":
            if self._require_auth():
                self._serve_events()
            return
        self._serve_static(parsed.path)

    def _serve_static(self, url_path: str) -> None:
        relative = "index.html" if url_path in ("", "/") else url_path.lstrip("/")
        candidate = (WEB_DIR / relative).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_events(self) -> None:
        target = self.server.manager.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while not self.server.manager.shutting_down.is_set():
                try:
                    event = target.get(timeout=12.0)
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"event: update\ndata: {payload}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.manager.unsubscribe(target)

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        parsed = self._parsed()
        path = parsed.path.rstrip("/")
        manager = self.server.manager

        if path == "/api/all/start":
            accepted = manager.start_sequence_async(False)
            if accepted:
                self._json(HTTPStatus.ACCEPTED, {"ok": True})
            else:
                self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "另一个整体操作仍在进行"})
            return
        if path == "/api/all/restart":
            accepted = manager.start_sequence_async(True)
            if accepted:
                self._json(HTTPStatus.ACCEPTED, {"ok": True})
            else:
                self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "另一个整体操作仍在进行"})
            return
        if path == "/api/all/stop":
            accepted = manager.stop_all_async()
            if accepted:
                self._json(HTTPStatus.ACCEPTED, {"ok": True})
            else:
                self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "另一个整体操作仍在进行"})
            return
        if path == "/api/all/cleanup":
            threading.Thread(target=manager.cleanup_all, daemon=True).start()
            self._json(HTTPStatus.ACCEPTED, {"ok": True})
            return

        match = re.fullmatch(r"/api/task/([a-z0-9_-]+)/(start|stop|restart|clear)", path)
        if match:
            task_id, action = match.groups()
            if task_id not in manager.tasks:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "未知任务"})
                return
            control_mode = None
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                try:
                    payload = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求参数不是有效 JSON"})
                    return
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求参数必须是对象"})
                    return
                control_mode = payload.get("control_mode")
            if action in ("start", "restart"):
                task = manager.tasks[task_id]
                allowed_modes = task.spec.get("control_modes", [])
                if allowed_modes:
                    control_mode = control_mode or task.spec.get("default_control_mode")
                    if control_mode not in allowed_modes:
                        self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "不支持的客户端控制模式"})
                        return
                elif control_mode is not None:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "此任务不接受控制模式参数"})
                    return

            def run_action() -> None:
                if action == "start":
                    manager.start_task(task_id, cleanup=True, control_mode=control_mode)
                elif action == "stop":
                    manager.cancel_start_sequence()
                    manager.stop_task(task_id, cleanup=True)
                elif action == "restart":
                    manager.cancel_start_sequence()
                    manager.restart_task(task_id, control_mode=control_mode)
                else:
                    manager.clear_logs(task_id)

            threading.Thread(target=run_action, daemon=True, name=f"api-{action}-{task_id}").start()
            self._json(HTTPStatus.ACCEPTED, {"ok": True})
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"})


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    required = {"id", "order", "title", "subtitle", "group", "run_as", "command"}
    seen: set[str] = set()
    for task in config.get("tasks", []):
        missing = required.difference(task)
        if missing:
            raise ValueError(f"任务配置缺少字段 {sorted(missing)}: {task}")
        if task["id"] in seen:
            raise ValueError(f"重复任务 id: {task['id']}")
        if task["run_as"] not in ("root", "operator"):
            raise ValueError(f"run_as 仅支持 root/operator: {task['id']}")
        if "readiness_command" in task:
            timeout = float(task.get("startup_timeout_sec", 20.0))
            if not isinstance(task["readiness_command"], str) or not task["readiness_command"].strip():
                raise ValueError(f"readiness_command 必须为非空命令: {task['id']}")
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError(f"startup_timeout_sec 必须为有限正数: {task['id']}")
        modes = task.get("control_modes", [])
        if modes and task.get("default_control_mode") not in modes:
            raise ValueError(f"默认控制模式必须属于 control_modes: {task['id']}")
        seen.add(task["id"])
    if not seen:
        raise ValueError("tasks.json 中没有任务")
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Teleoperation Control Center")
    parser.add_argument("--host", default=os.environ.get("TELEOP_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HUMANEGO_PORT", "8876")))
    parser.add_argument("--operator", default=os.environ.get("TELEOP_OPERATOR_USER", ""))
    parser.add_argument("--operator-home", default=os.environ.get("TELEOP_OPERATOR_HOME", ""))
    parser.add_argument("--no-auto-start", action="store_true")
    return parser.parse_args()


def main() -> int:
    if not sys.platform.startswith("linux"):
        print("此控制台仅支持 Linux 服务器。", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("请通过 ./start_console.sh 启动（它会请求一次 sudo）。", file=sys.stderr)
        return 2

    args = parse_args()
    operator = args.operator or os.environ.get("SUDO_USER", "")
    if not operator or operator == "root":
        print("无法确定普通用户。请使用 --operator <用户名> 指定。", file=sys.stderr)
        return 2
    try:
        record = pwd.getpwnam(operator)
    except KeyError:
        print(f"普通用户不存在：{operator}", file=sys.stderr)
        return 2
    operator_home = args.operator_home or record.pw_dir

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("安全限制：默认只允许监听本机。远程查看请使用 README 中的 SSH 隧道。", file=sys.stderr)
        return 2

    token = os.environ.get("TELEOP_TOKEN") or secrets.token_urlsafe(24)
    replace_previous_controller()
    manager = TeleopManager(load_config(), operator, operator_home)
    try:
        server = TeleopHTTPServer((args.host, args.port), manager, token)
    except OSError as exc:
        print(f"控制台端口启动失败（{args.host}:{args.port}）：{exc}", file=sys.stderr)
        print("可尝试：HUMANEGO_PORT=8877 ./start_console.sh", file=sys.stderr)
        return 2
    write_pid_file()
    url_host = "127.0.0.1" if args.host in ("localhost", "::1") else args.host
    url = f"http://{url_host}:{server.server_port}/?token={urllib.parse.quote(token)}"

    print("\n" + "=" * 72)
    print("  遥操作控制台已启动")
    print(f"  操作用户：{operator}")
    print(f"  控制页面：{url}")
    print("  按 Ctrl+C 将逆序停止全部任务并退出")
    print("=" * 72 + "\n", flush=True)

    if not args.no_auto_start:
        threading.Timer(0.8, manager.start_sequence_async).start()

    stopping = threading.Event()

    def handle_signal(_signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        manager.shutdown()
        server.server_close()
        remove_pid_file()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
