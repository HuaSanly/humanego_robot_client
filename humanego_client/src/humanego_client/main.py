from __future__ import annotations

import logging
import time

import numpy as np

from .action_safety import ActionSafety
from .camera import USBStereoCamera
from .cli import apply_control_mode, parse_args
from .config import load_calibrations, load_config, load_yaml
from .ik import RightArmIK, URDFFrameFK, joint_feedback_map, motor_command_from_feedback, solve_ik_with_logging
from .remote import connect_remote
from .ros_bridge import RosBridgeClient
from .runtime import RemoteChunkRunner
from .transforms import ee_state_in_camera, rectified_optical_from_base, urdf_ee_target_from_hand, validate_transform

LOG = logging.getLogger("humanego_client")


def run(config_path: str, control_mode: str | None = None):
    config, _ = load_config(config_path)
    apply_control_mode(config, control_mode)
    camera_cfg = load_yaml(config["camera_config"])
    stereo, hand_eye = load_calibrations(config["calibration_dir"])
    camera = USBStereoCamera(camera_cfg, stereo)
    bridge = RosBridgeClient(config["ipc"]["endpoint"], int(config["ipc"]["timeout_ms"]))
    robot_cfg = config["robot"]
    T_urdf_ee_from_hand = validate_transform(
        robot_cfg["T_urdf_ee_from_hand"], "T_urdf_ee_from_hand"
    )
    ik = RightArmIK(robot_cfg["urdf_path"], robot_cfg["base_frame"], robot_cfg["right_ee_frame"],
                    robot_cfg["active_joint_names"])
    camera_fk = URDFFrameFK(
        robot_cfg["urdf_path"], robot_cfg["base_frame"], hand_eye["urdf_camera_frame"]
    )
    T_optical_from_urdf_camera = validate_transform(
        hand_eye["T_optical_from_urdf_camera"], "T_optical_from_urdf_camera"
    )
    remote = runner = None
    safety = ActionSafety(config["control"], np.eye(4))
    T_rectified_optical_from_base = None
    control = config["control"]
    period = 1.0 / float(control["action_hz"])
    next_tick = time.monotonic()
    try:
        LOG.info(
            "connecting remote endpoint=%s deployment=%s task=%s",
            config["server"].get("endpoint", "<server config>"),
            config["server"]["deployment"],
            config["server"]["task"],
        )
        try:
            remote, remote_cfg = connect_remote(config["server"], config["server"]["task"])
        except Exception:
            LOG.exception(
                "remote connection failed endpoint=%s",
                config["server"].get("endpoint", "<server config>"),
            )
            raise
        runner = RemoteChunkRunner(remote, config["server"], config["server"]["task"])
        LOG.info(
            "connected; control mode=%s action_hz=%.2f action_timeout_s=%.2f",
            control["mode"],
            float(control["action_hz"]),
            float(config["server"]["action_timeout_s"]),
        )
        while True:
            frame = camera.read()
            feedback_state = bridge.get_state(float(config["ipc"]["max_state_age_s"]))
            if abs(frame["captured_at"] - feedback_state["captured_at"]) > float(config["server"]["max_observation_age_s"]):
                raise TimeoutError("camera frame and robot state are not synchronized")
            joint_values = joint_feedback_map(feedback_state["feedback"])
            # Head feedback is used only to locate the moving camera for the
            # current observation. Head joints are never action targets.
            T_base_from_camera_link = camera_fk.forward(joint_values)
            T_rectified_optical_from_base = rectified_optical_from_base(
                T_base_from_camera_link,
                T_optical_from_urdf_camera,
                camera.left_rectification_rotation,
            )
            safety.update_camera_transform(T_rectified_optical_from_base)
            LOG.debug(
                "camera frame updated frame=%s optical_origin_base_m=%s",
                hand_eye["urdf_camera_frame"],
                np.round(T_base_from_camera_link[:3, 3], 4).tolist(),
            )
            T_base_from_ee = ik.forward(joint_values)
            T_base_from_hand = T_base_from_ee @ T_urdf_ee_from_hand
            gripper = float(np.clip(1.0 - feedback_state["feedback"]["feetech"][9] /
                                    float(robot_cfg["gripper_open_angle_rad"]), 0.0, 1.0))
            state_vec = ee_state_in_camera(T_rectified_optical_from_base, T_base_from_hand, gripper)
            features = {
                "observation.images.head_left": frame["left"],
                "observation.images.head_right": frame["right"],
                "observation.state": state_vec,
            }
            runner.request_if_needed(features, frame["captured_at"])
            runner.log_queue_summary()
            now = time.monotonic()
            if now >= next_tick:
                action = runner.pop_action()
                if action is not None:
                    action_index = runner.actions_popped
                    try:
                        target_hand, grasp = safety.filter(action, T_base_from_hand)
                    except Exception:
                        LOG.exception("action safety rejected action=%d", action_index)
                        raise
                    LOG.info(
                        "action accepted index=%d grasp=%.3f",
                        action_index,
                        grasp,
                    )
                    target_ee = urdf_ee_target_from_hand(target_hand, T_urdf_ee_from_hand)
                    joint_targets = solve_ik_with_logging(
                        ik,
                        target_ee,
                        joint_values,
                        float(control["max_joint_delta_rad"]),
                        action_index,
                    )
                    command = motor_command_from_feedback(feedback_state["feedback"], joint_targets, grasp,
                                                          float(robot_cfg["gripper_open_angle_rad"]))
                    try:
                        bridge.send_command(command, control["mode"])
                    except Exception:
                        LOG.exception(
                            "command send failed action=%d mode=%s motor_count=%d",
                            action_index,
                            control["mode"],
                            len(command),
                        )
                        raise
                    LOG.info(
                        "command acknowledged action=%d mode=%s motor_count=%d controlled_joints=%d",
                        action_index,
                        control["mode"],
                        len(command),
                        len(joint_targets),
                    )
                next_tick += period
                if now - next_tick > period:
                    next_tick = now + period
            time.sleep(max(0.0, min(0.005, next_tick - time.monotonic())))
    finally:
        LOG.info("stopping client")
        safety.stop()
        if runner is not None:
            runner.close()
        if remote is not None:
            try:
                remote.close()
            except Exception:
                LOG.exception("error closing remote session")
        bridge.close()
        camera.close()


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    run(args.config, args.control_mode)


if __name__ == "__main__":
    main()
