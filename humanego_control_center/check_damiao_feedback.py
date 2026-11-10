#!/usr/bin/env python3
"""Subscribe to Damiao feedback without sending any motor commands."""

import argparse
import math
import sys
import threading
import time


def feedback_error(message, expected_joints=8, publisher="/damiao_motor_node"):
    connection = getattr(message, "_connection_header", {}) or {}
    if connection.get("callerid") != publisher:
        return "反馈并非来自 {}".format(publisher)
    if connection.get("latching") == "1":
        return "收到缓存反馈，仍需实时反馈"
    if len(message.position) != expected_joints:
        return "反馈关节数为 {}，期望 {}".format(len(message.position), expected_joints)
    if not all(math.isfinite(value) for value in message.position):
        return "关节位置包含非有限数值"
    return ""


def wait_for_feedback(rospy, message_type, topic, timeout):
    ready = threading.Event()
    consecutive = 0
    reason = "尚未收到反馈"
    lock = threading.Lock()

    def receive(message):
        nonlocal consecutive, reason
        with lock:
            reason = feedback_error(message)
            consecutive = 0 if reason else consecutive + 1
            if consecutive >= 2:
                ready.set()
            elif not reason:
                reason = "仅收到一条有效反馈，等待下一条"

    deadline = time.monotonic() + timeout
    subscriber = rospy.Subscriber(topic, message_type, receive, queue_size=10)
    try:
        while not rospy.is_shutdown():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if ready.wait(min(0.1, remaining)):
                print("[Damiao] 收到达妙节点连续两条有效的 8 关节反馈", flush=True)
                return 0
        with lock:
            print("[Damiao] {}s 内未通过反馈检查：{} ({})".format(timeout, reason, topic),
                  file=sys.stderr, flush=True)
        return 1
    finally:
        subscriber.unregister()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default="/feedback_damiao_joint_states")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")
    try:
        import rospy
        from sensor_msgs.msg import JointState

        rospy.init_node("damiao_startup_check", anonymous=True, disable_signals=True)
        return wait_for_feedback(rospy, JointState, args.topic, args.timeout)
    except Exception as exc:
        print("[Damiao] 无法检查反馈：{}".format(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
