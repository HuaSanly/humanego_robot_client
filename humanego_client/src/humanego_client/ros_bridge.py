from __future__ import annotations

import time
import uuid

import numpy as np
import zmq


class RosBridgeClient:
    """ZeroMQ client for a ROS-side adapter; no ROS imports enter this environment."""

    def __init__(self, endpoint: str, timeout_ms: int = 100):
        self.context = zmq.Context.instance()
        self.socket = self.context.socket(zmq.REQ)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self.socket.setsockopt(zmq.SNDTIMEO, timeout_ms)
        self.socket.connect(endpoint)
        self.sequence = 0

    def _request(self, operation: str, **payload):
        self.sequence += 1
        request = {"protocol": "humanego_ros_bridge_v1", "id": uuid.uuid4().hex,
                   "sequence": self.sequence, "operation": operation, **payload}
        try:
            self.socket.send_json(request)
            reply = self.socket.recv_json()
        except zmq.error.Again as exc:
            raise TimeoutError(f"ROS bridge {operation} timed out") from exc
        if reply.get("id") != request["id"]:
            raise RuntimeError("ROS bridge response ID mismatch")
        if not reply.get("ok", False):
            raise RuntimeError(f"ROS bridge rejected {operation}: {reply.get('error', 'unknown error')}")
        return reply

    def get_state(self, max_age_s: float) -> dict:
        reply = self._request("get_state")
        state = reply.get("feedback", {})
        now = time.monotonic()
        stamp = float(state["captured_at_monotonic"])
        if now - stamp > max_age_s or stamp > now + 0.01:
            raise TimeoutError("ROS robot state is stale or from an incompatible monotonic clock")
        feedback = {name: np.asarray(state[name], dtype=np.float64) for name in ("damiao", "feetech")}
        expected = {"damiao": 8, "feetech": 10}
        for name, positions in feedback.items():
            if positions.shape != (expected[name],) or not np.isfinite(positions).all():
                raise ValueError(f"ROS adapter returned invalid {name} feedback")
        return {"feedback": feedback, "captured_at": stamp}

    def send_command(self, motor_positions: np.ndarray, mode: str) -> None:
        if mode not in {"record_only", "execute"}:
            raise ValueError(f"unsupported control mode {mode}")
        operation = "record_command" if mode == "record_only" else "publish_command"
        self._request(operation, motor_positions=np.asarray(motor_positions, dtype=np.float64).tolist())

    def close(self) -> None:
        self.socket.close(linger=0)
