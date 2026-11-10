#!/usr/bin/env python3
"""ROS topic-only bridge for raw motor feedback and full motor commands."""

from __future__ import annotations

import math
import threading
import time

import rospy
import zmq
from sensor_msgs.msg import JointState


class TopicBridge:
    def __init__(self):
        self.endpoint = rospy.get_param("~listen_endpoint", "tcp://127.0.0.1:5557")
        self.max_age = float(rospy.get_param("~feedback_timeout_s", 0.2))
        self.max_skew = float(rospy.get_param("~feedback_source_skew_s", 0.1))
        self.names = rospy.get_param("~motor_command_names", [])
        if len(self.names) != 21:
            raise ValueError("motor_command_names must define the fixed 21-motor command layout")
        topics = rospy.get_param("~feedback_topics", {})
        self.topic_names = {
            "damiao": topics.get("damiao", "/feedback_damiao_joint_states"),
            "feetech": topics.get("feetech", "/feedback_feetech_joint_states"),
        }
        self.feedback = {}
        self.lock = threading.Lock()
        self.subscribers = [
            rospy.Subscriber(self.topic_names["damiao"], JointState, self._callback, "damiao", queue_size=10),
            rospy.Subscriber(self.topic_names["feetech"], JointState, self._callback, "feetech", queue_size=10),
        ]
        self.command_topic = rospy.get_param("~command_topic", "/command_joint_states")
        self.command_pub = rospy.Publisher(self.command_topic, JointState, queue_size=1)
        self.socket = zmq.Context.instance().socket(zmq.REP)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.bind(self.endpoint)
        rospy.loginfo("HumanEgo topic bridge listening at %s", self.endpoint)

    def _callback(self, message, source):
        expected = {"damiao": 8, "feetech": 10}[source]
        values = [float(x) for x in message.position[:expected]]
        if len(values) != expected or not all(math.isfinite(x) for x in values):
            rospy.logwarn_throttle(2.0, "invalid %s feedback JointState", source)
            return
        with self.lock:
            self.feedback[source] = {"positions": values, "received_at": time.monotonic()}

    def _feedback_snapshot(self):
        now = time.monotonic()
        with self.lock:
            snapshot = {key: dict(value) for key, value in self.feedback.items()}
        if set(snapshot) != {"damiao", "feetech"}:
            raise TimeoutError("both arm and head feedback topics must produce data")
        stamps = [snapshot[key]["received_at"] for key in ("damiao", "feetech")]
        if any(now - stamp > self.max_age for stamp in stamps):
            raise TimeoutError("motor feedback is stale")
        if max(stamps) - min(stamps) > self.max_skew:
            raise TimeoutError("motor feedback topic timestamps exceed configured skew")
        return {"captured_at_monotonic": max(stamps),
                "damiao": snapshot["damiao"]["positions"],
                "feetech": snapshot["feetech"]["positions"]}

    def _publish_command(self, request):
        values = request.get("motor_positions")
        if not isinstance(values, list) or len(values) != 21:
            raise ValueError("motor_positions must have length 21")
        positions = [float(value) for value in values]
        if not all(math.isfinite(value) for value in positions):
            raise ValueError("motor_positions must be finite")
        message = JointState()
        message.header.stamp = rospy.Time.now()
        message.name = list(self.names)
        message.position = positions
        self.command_pub.publish(message)
        return {"published": True}

    def _handle(self, request):
        if request.get("protocol") != "humanego_ros_bridge_v1":
            raise ValueError("unsupported bridge protocol")
        operation = request.get("operation")
        if operation == "get_state":
            return {"feedback": self._feedback_snapshot()}
        if operation in ("record_command", "publish_command"):
            if operation == "record_command":
                values = request.get("motor_positions")
                if not isinstance(values, list) or len(values) != 21:
                    raise ValueError("motor_positions must have length 21")
                if not all(math.isfinite(float(value)) for value in values):
                    raise ValueError("motor_positions must be finite")
                return {"recorded": True}
            return self._publish_command(request)
        raise ValueError("unsupported bridge operation")

    def spin(self):
        poller = zmq.Poller()
        poller.register(self.socket, zmq.POLLIN)
        while not rospy.is_shutdown():
            if not dict(poller.poll(100)):
                continue
            request = {}
            try:
                request = self.socket.recv_json()
                result = self._handle(request)
                response = {"id": request.get("id"), "ok": True, **result}
            except Exception as exc:
                response = {"id": request.get("id"), "ok": False, "error": str(exc)}
            self.socket.send_json(response)

    def close(self):
        self.socket.close(linger=0)


def main():
    rospy.init_node("humanego_ros_adapter")
    bridge = TopicBridge()
    try:
        bridge.spin()
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
