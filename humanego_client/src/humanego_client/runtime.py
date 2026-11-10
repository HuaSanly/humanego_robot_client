from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import time
import uuid

import numpy as np
import torch
from lerobot.inference import ChunkRuntime, ExecutionMode, ObservationSnapshot


LOG = logging.getLogger("humanego_client.runtime")


class RemoteChunkRunner:
    def __init__(self, client, config: dict, task: str):
        self.client = client
        self.task = task
        self.runtime = ChunkRuntime(
            mode=ExecutionMode.CHUNK,
            action_interval=float(config["action_interval_s"]),
            refill_seconds=float(config["refill_seconds"]),
            max_observation_age_s=float(config["max_observation_age_s"]),
            action_timeout_s=float(config["action_timeout_s"]),
            startup_timeout_s=float(config["startup_timeout_s"]),
            action_starvation_grace_s=float(config["action_starvation_grace_s"]),
            chunk_merge="append",
        )
        self.runtime.activate()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="humanego-infer")
        self.future = None
        self.request = None
        self.started_at = time.monotonic()
        self.last_queue_log_at = self.started_at
        self.action_interval = float(config["action_interval_s"])
        self.max_observation_age = float(config["max_observation_age_s"])
        self.actions_popped = 0
        self.has_received_actions = False

    def request_if_needed(self, features: dict, captured_at: float):
        if self.future is not None and self.future.done():
            request = self.request
            try:
                result = self.future.result()
            except Exception:
                LOG.exception(
                    "remote inference failed observation_id=%s request_id=%s elapsed_s=%.3f",
                    request.observation.observation_id,
                    request.request_id,
                    time.monotonic() - request.submitted_at,
                )
                raise
            elapsed = time.monotonic() - request.submitted_at
            action_count = len(result.canonical_actions)
            accepted = self.runtime.accept(request, result, task_version=0)
            LOG.info(
                "inference result observation_id=%s request_id=%s elapsed_s=%.3f actions=%d accepted=%s queue_actions=%d",
                request.observation.observation_id,
                request.request_id,
                elapsed,
                action_count,
                accepted,
                self.runtime.queue.qsize(),
            )
            if not accepted:
                raise RuntimeError("discarded stale inference result")
            self.future = self.request = None
        if self.future is None and self.runtime.should_request():
            snapshot = ObservationSnapshot(features, captured_at, self.task, observation_id=uuid.uuid4().hex)
            self.request = self.runtime.begin(snapshot)
            if self.request is not None:
                state = features.get("observation.state")
                state_size = int(np.asarray(state).size) if state is not None else 0
                LOG.info(
                    "submitting observation observation_id=%s state_values=%d capture_age_s=%.3f queue_actions=%d",
                    snapshot.observation_id,
                    state_size,
                    time.monotonic() - captured_at,
                    self.runtime.queue.qsize(),
                )
                self.future = self.executor.submit(self.client.infer, self.request)
        if self.runtime.failure is not None:
            raise RuntimeError(f"remote chunk runtime failed: {self.runtime.failure}")

    def pop_action(self):
        self._discard_expired_actions()
        item = self.runtime.pop()
        if item is None:
            return None
        action = item[0]
        if isinstance(action, torch.Tensor):
            action = action.detach().cpu().numpy()
        action = np.asarray(action, dtype=np.float32)
        if action.shape != (10,):
            raise ValueError(f"runtime produced action with unexpected shape {action.shape}")
        self.actions_popped += 1
        LOG.debug(
            "action dequeued index=%d queue_actions=%d",
            self.actions_popped,
            self.runtime.queue.qsize(),
        )
        self.has_received_actions = True
        return action

    def _discard_expired_actions(self) -> int:
        """Remove expired queued endpoints before ChunkRuntime can latch a fault."""
        now = time.monotonic()
        runtime = self.runtime
        queue = runtime.queue
        dropped = 0
        expired_current_age = None
        with runtime.lock:
            if runtime.failure is not None:
                return 0
            if runtime.current is not None:
                age = now - runtime.current.capture_time
                if age > self.max_observation_age:
                    expired_current_age = age
                    runtime.current = None
            with queue.lock:
                if queue.queue is not None and queue._provenance_queue:
                    remaining_start = queue.last_index
                    remaining_provenance = queue._provenance_queue[remaining_start:]
                    for provenance in remaining_provenance:
                        if provenance is None or now - provenance.capture_time <= self.max_observation_age:
                            break
                        dropped += 1
                    if dropped:
                        offset = remaining_start + dropped
                        queue.queue = queue.queue[offset:].clone()
                        if queue.original_queue is not None:
                            queue.original_queue = queue.original_queue[offset:].clone()
                        if queue._task_queue is not None:
                            queue._task_queue = queue._task_queue[offset:]
                        queue._provenance_queue = queue._provenance_queue[offset:]
                        queue.last_index = 0
                        queue._cursor += dropped
        if dropped or expired_current_age is not None:
            LOG.warning(
                "expired actions discarded queued=%d current_source_age_s=%s remaining=%d",
                dropped,
                f"{expired_current_age:.3f}" if expired_current_age is not None else "none",
                queue.qsize(),
            )
        return dropped

    def log_queue_summary(self):
        now = time.monotonic()
        if now - self.last_queue_log_at < 1.0:
            return
        self.last_queue_log_at = now
        queued = self.runtime.queue.qsize()
        LOG.info(
            "action queue queued=%d playback_s=%.3f actions_popped=%d",
            queued,
            queued * self.action_interval,
            self.actions_popped,
        )

    def close(self):
        self.runtime.invalidate(held=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
