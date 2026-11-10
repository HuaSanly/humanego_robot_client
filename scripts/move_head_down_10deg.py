#!/usr/bin/env python3
"""Move Head03 to an absolute downward camera angle.

The motor driver consumes a fixed 21-position JointState layout.  This script
reads available arm/head feedback and rebuilds that layout on every publish
so only command position 12 (Head03) is changed. Feetech feedback is required;
Damiao feedback is optional. Waist command slots remain at the fixed zero pose.
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from typing import Dict, List, Tuple

import rosgraph
import rospy
from sensor_msgs.msg import JointState


COMMAND_TOPIC = os.environ.get("HEAD_COMMAND_TOPIC", "/command_joint_states")
DAMIAO_TOPIC = os.environ.get(
    "HEAD_DAMIAO_FEEDBACK_TOPIC", "/feedback_damiao_joint_states"
)
FEETECH_TOPIC = os.environ.get(
    "HEAD_FEETECH_FEEDBACK_TOPIC", "/feedback_feetech_joint_states"
)
DRY_RUN = os.environ.get("HEAD_DRY_RUN", "0").lower() in {"1", "true", "yes"}
HEAD_TARGET_DEG = float(os.environ.get("HEAD_TARGET_DEG", "30.0"))

FEEDBACK_WAIT_S = float(os.environ.get("HEAD_FEEDBACK_WAIT_S", "5.0"))
FEEDBACK_MAX_AGE_S = float(os.environ.get("HEAD_FEEDBACK_MAX_AGE_S", "0.5"))
SETTLE_TIMEOUT_S = float(os.environ.get("HEAD_SETTLE_TIMEOUT_S", "12.0"))
PUBLISH_HZ = 10.0
SETTLE_TOLERANCE_RAD = 0.02
SETTLE_SAMPLES = 8
HEAD03_TARGET_RAD = math.radians(HEAD_TARGET_DEG)
HEAD03_LIMITS = (-1.0, 1.0)
WAIST_ZERO_POSITION = [0.0, 0.0, 0.0]

COMMAND_NAMES = (
    "eyou_motor1",
    "eyou_motor2",
    "eyou_motor3",
    "damiao_motor1",
    "damiao_motor2",
    "damiao_motor3",
    "damiao_motor4",
    "damiao_motor5",
    "damiao_motor6",
    "damiao_motor7",
    "damiao_motor8",
    "feetech_motor1",
    "feetech_motor2",
    "feetech_motor3",
    "feetech_motor4",
    "feetech_motor5",
    "feetech_motor6",
    "feetech_motor7",
    "feetech_motor8",
    "feetech_motor9",
    "feetech_motor10",
)


class FeedbackBuffer:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._values: Dict[str, Tuple[List[float], float]] = {}

    def callback(self, message: JointState, callback_args: Tuple[str, int]) -> None:
        source, required = callback_args
        values = [float(value) for value in message.position[:required]]
        if len(message.position) < required:
            rospy.logwarn_throttle(
                2.0,
                "[move_head_down_10deg] %s feedback has %d positions; need %d",
                source,
                len(message.position),
                required,
            )
            return
        if not all(math.isfinite(value) for value in values):
            rospy.logwarn_throttle(
                2.0,
                "[move_head_down_10deg] %s feedback contains NaN or Inf",
                source,
            )
            return
        with self._condition:
            self._values[source] = (values, time.monotonic())
            self._condition.notify_all()

    def snapshot(self, max_age_s: float) -> Dict[str, List[float]]:
        now = time.monotonic()
        with self._condition:
            if "feetech" not in self._values:
                raise RuntimeError("missing feedback: feetech")
            if now - self._values["feetech"][1] > max_age_s:
                raise RuntimeError("stale feedback: feetech")
            values = {"feetech": list(self._values["feetech"][0])}
            if "damiao" in self._values and now - self._values["damiao"][1] <= max_age_s:
                values["damiao"] = list(self._values["damiao"][0])
            else:
                values["damiao"] = [0.0] * 8
            return values

    def wait_for_snapshot(self, timeout_s: float, max_age_s: float) -> Dict[str, List[float]]:
        deadline = time.monotonic() + timeout_s
        last_error = "feedback not available"
        while not rospy.is_shutdown() and time.monotonic() < deadline:
            try:
                return self.snapshot(max_age_s)
            except RuntimeError as error:
                last_error = str(error)
            with self._condition:
                remaining = max(0.0, deadline - time.monotonic())
                self._condition.wait(timeout=min(0.1, remaining))
        raise TimeoutError(last_error)


def build_command(values: Dict[str, List[float]], head03_target: float) -> List[float]:
    command = WAIST_ZERO_POSITION + values["damiao"] + values["feetech"]
    if len(command) != len(COMMAND_NAMES):
        raise RuntimeError(f"feedback command layout has {len(command)} values; need 21")
    if not math.isfinite(head03_target):
        raise RuntimeError("Head03 target is not finite")
    command[12] = head03_target
    return command


def topic_has_publisher(topic: str) -> bool:
    if not topic:
        return False
    master = rosgraph.Master(rospy.get_name())
    publishers, _, _ = master.getSystemState()
    return any(name == topic and nodes for name, nodes in publishers)


def main() -> int:
    if FEEDBACK_WAIT_S <= 0 or FEEDBACK_MAX_AGE_S <= 0 or SETTLE_TIMEOUT_S <= 0:
        raise ValueError("feedback and settle timeouts must be positive")
    if not math.isfinite(HEAD_TARGET_DEG):
        raise ValueError("HEAD_TARGET_DEG must be finite")
    lower, upper = HEAD03_LIMITS
    if not lower <= HEAD03_TARGET_RAD <= upper:
        raise ValueError("Head03 target is outside the configured URDF limits")

    rospy.init_node("move_head_down_10deg", anonymous=False)
    buffer = FeedbackBuffer()
    if not topic_has_publisher(FEETECH_TOPIC):
        raise RuntimeError(
            f"required Feetech feedback publisher is absent: {FEETECH_TOPIC}"
        )
    rospy.Subscriber(FEETECH_TOPIC, JointState, buffer.callback, ("feetech", 10), queue_size=10)
    if DAMIAO_TOPIC:
        rospy.Subscriber(DAMIAO_TOPIC, JointState, buffer.callback, ("damiao", 8), queue_size=10)
    rospy.loginfo(
        "[move_head_down_10deg] absolute target Head03=%.6f rad (%.3f deg)",
        HEAD03_TARGET_RAD,
        HEAD_TARGET_DEG,
    )

    values = buffer.wait_for_snapshot(FEEDBACK_WAIT_S, FEEDBACK_MAX_AGE_S)
    current_head03 = values["feetech"][1]

    rospy.loginfo(
        "[move_head_down_10deg] Head03 %.6f -> %.6f rad (%.3f deg); preserving other feedback values",
        current_head03,
        HEAD03_TARGET_RAD,
        HEAD_TARGET_DEG,
    )
    if DRY_RUN:
        command = build_command(values, HEAD03_TARGET_RAD)
        rospy.loginfo("[move_head_down_10deg] dry run command=%s", command)
        return 0

    publisher = rospy.Publisher(COMMAND_TOPIC, JointState, queue_size=10)
    rate = rospy.Rate(PUBLISH_HZ)
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    consecutive = 0

    while not rospy.is_shutdown() and time.monotonic() < deadline:
        values = buffer.snapshot(FEEDBACK_MAX_AGE_S)
        command = build_command(values, HEAD03_TARGET_RAD)
        message = JointState()
        message.header.stamp = rospy.Time.now()
        message.name = list(COMMAND_NAMES)
        message.position = command
        publisher.publish(message)

        error = abs(values["feetech"][1] - HEAD03_TARGET_RAD)
        consecutive = consecutive + 1 if error <= SETTLE_TOLERANCE_RAD else 0
        rospy.loginfo_throttle(
            1.0,
            "[move_head_down_10deg] Head03 feedback=%.6f target=%.6f error=%.6f",
            values["feetech"][1],
            HEAD03_TARGET_RAD,
            error,
        )
        if consecutive >= SETTLE_SAMPLES:
            rospy.loginfo("[move_head_down_10deg] Head03 reached target")
            return 0
        rate.sleep()

    raise TimeoutError(
        f"Head03 did not reach {HEAD_TARGET_DEG:.3f} degrees before the timeout"
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Exception, rospy.ROSInterruptException) as error:
        if rospy.core.is_initialized():
            rospy.logfatal("[move_head_down_10deg] %s", error)
        else:
            print(f"[move_head_down_10deg] {error}", file=sys.stderr)
        sys.exit(1)
