from __future__ import annotations

import logging
import time

import numpy as np
import xml.etree.ElementTree as ET


LOG = logging.getLogger("humanego_client")


DAMIAO_JOINTS = (
    "ArmL02_Joint", "ArmL03_Joint", "ArmL04_Joint", "ArmL05_Joint",
    "ArmR02_Joint", "ArmR03_Joint", "ArmR04_Joint", "ArmR05_Joint",
)
FEETECH_JOINTS = (
    "Head02_Joint", "Head03_Joint", "ArmL06_Joint", "ArmL07_Joint",
    "ArmL08_Joint", "ArmR06_Joint", "ArmR07_Joint", "ArmR08_Joint",
)
WAIST_JOINTS = ("FoldingModularJoint02_Joint", "FoldingModularJoint03_Joint", "Trunk_Joint")
WAIST_ZERO_POSITION = (0.0, 0.0, 0.0)
RIGHT_ARM_JOINTS = ("ArmR02_Joint", "ArmR03_Joint", "ArmR04_Joint", "ArmR05_Joint",
                    "ArmR06_Joint", "ArmR07_Joint", "ArmR08_Joint")
MOTOR_COMMAND_INDEX = {
    "ArmR02_Joint": 7, "ArmR03_Joint": 8, "ArmR04_Joint": 9,
    "ArmR05_Joint": 10, "ArmR06_Joint": 16, "ArmR07_Joint": 17,
    "ArmR08_Joint": 18,
}
IK_POSITION_RESIDUAL_TOLERANCE_M = 0.12
IK_ROTATION_RESIDUAL_TOLERANCE_RAD = 0.20


def solve_ik_with_logging(ik, target_ee, joint_values, max_joint_delta_rad, action_index):
    started_at = time.monotonic()
    try:
        joint_targets = ik.solve(target_ee, joint_values, max_joint_delta_rad)
    except Exception:
        LOG.exception(
            "IK failed action=%d target_position_m=%s max_joint_delta_rad=%.4f",
            action_index,
            np.round(target_ee[:3, 3], 4).tolist(),
            max_joint_delta_rad,
        )
        raise
    joint_delta = {
        name: round(float(value) - float(joint_values[name]), 4)
        for name, value in joint_targets.items()
    }
    LOG.info(
        "IK solved action=%d elapsed_ms=%.1f target_position_m=%s joints=%s delta_rad=%s",
        action_index,
        (time.monotonic() - started_at) * 1000.0,
        np.round(target_ee[:3, 3], 4).tolist(),
        {name: round(float(value), 4) for name, value in joint_targets.items()},
        joint_delta,
    )
    return joint_targets


def joint_feedback_map(feedback: dict) -> dict[str, float]:
    positions = dict(zip(DAMIAO_JOINTS, feedback["damiao"].tolist()))
    positions.update(zip(FEETECH_JOINTS, feedback["feetech"][:8].tolist()))
    positions.update(zip(WAIST_JOINTS, WAIST_ZERO_POSITION))
    return positions


def motor_command_from_feedback(feedback: dict, joint_targets: dict[str, float], gripper_closed: float,
                                gripper_open_angle: float) -> np.ndarray:
    command = np.concatenate((WAIST_ZERO_POSITION, feedback["damiao"], feedback["feetech"])).astype(np.float64)
    if command.shape != (21,) or not np.isfinite(command).all():
        raise ValueError("feedback must form a finite 21-motor vector")
    if gripper_closed not in (0.0, 1.0) or gripper_open_angle <= 0:
        raise ValueError("invalid gripper target calibration")
    for name, value in joint_targets.items():
        if name not in MOTOR_COMMAND_INDEX or not np.isfinite(value):
            raise ValueError(f"unsupported or invalid joint target {name}")
        command[MOTOR_COMMAND_INDEX[name]] = float(value)
    command[20] = (1.0 - gripper_closed) * gripper_open_angle
    return command


def urdf_chain_elements(urdf_path: str, base_link: str, tip_link: str) -> list[str]:
    root = ET.parse(urdf_path).getroot()
    parents = {}
    for joint in root.findall("joint"):
        parent = joint.find("parent").get("link")
        child = joint.find("child").get("link")
        parents[child] = (parent, joint.get("name"))
    chain = []
    link = tip_link
    while link != base_link:
        if link not in parents:
            raise ValueError(f"URDF has no path from {base_link} to {tip_link}")
        parent, joint = parents[link]
        chain.append((joint, link))
        link = parent
    elements = [base_link]
    for joint, child in reversed(chain):
        elements.extend((joint, child))
    return elements


class RightArmIK:
    """URDF-based right-arm FK and IK kept entirely in the HumanEgo process."""

    def __init__(self, urdf_path: str, base_link: str, tip_link: str, active_joint_names: list[str]):
        from ikpy.chain import Chain

        elements = urdf_chain_elements(urdf_path, base_link, tip_link)
        self.chain = Chain.from_urdf_file(urdf_path, base_elements=elements)
        self.link_names = [getattr(link, "name", "") for link in self.chain.links]
        tip_joint_name = elements[-2]
        if tip_joint_name not in self.link_names:
            raise ValueError(f"IK chain does not include the joint leading to {tip_link}")
        tip_index = self.link_names.index(tip_joint_name)
        self.chain.links = self.chain.links[:tip_index + 1]
        self.chain._length = sum(link.length for link in self.chain.links)
        self.link_names = self.link_names[:tip_index + 1]
        if len(active_joint_names) != len(RIGHT_ARM_JOINTS) or set(active_joint_names) != set(RIGHT_ARM_JOINTS):
            raise ValueError("active_joint_names must name the seven right arm URDF joints")
        self.active_joint_names = list(active_joint_names)
        missing = sorted(set(self.active_joint_names) - set(self.link_names))
        if missing:
            raise ValueError("right arm joints missing from IK chain: " + ", ".join(missing))
        self.active_indices = [self.link_names.index(name) for name in self.active_joint_names]
        self.chain.active_links_mask = np.asarray(
            [False] + [name in self.active_joint_names for name in self.link_names[1:]], dtype=bool
        )
        self.joint_limits = {name: tuple(map(float, self.chain.links[index].bounds))
                             for name, index in zip(self.active_joint_names, self.active_indices)}

    def _seed(self, joint_values: dict[str, float]) -> np.ndarray:
        seed = np.zeros(len(self.chain.links), dtype=np.float64)
        for index, name in enumerate(self.link_names):
            if name in joint_values:
                seed[index] = joint_values[name]
        return seed

    def forward(self, joint_values: dict[str, float]) -> np.ndarray:
        T = np.asarray(self.chain.forward_kinematics(self._seed(joint_values)), dtype=np.float64)
        if T.shape != (4, 4) or not np.isfinite(T).all():
            raise RuntimeError("IKPy forward kinematics returned invalid pose")
        return T

    def solve(self, T_base_from_ee: np.ndarray, current: dict[str, float], max_delta_rad: float) -> dict[str, float]:
        target = np.asarray(T_base_from_ee, dtype=np.float64)
        if target.shape != (4, 4) or not np.isfinite(target).all():
            raise ValueError("IK target must be a finite 4x4 transform")
        seed = self._seed(current)
        solved = self.chain.inverse_kinematics_frame(target, initial_position=seed, orientation_mode="all")
        result = {}
        for name, index in zip(self.active_joint_names, self.active_indices):
            value = float(solved[index])
            if not np.isfinite(value):
                raise ValueError(f"IK result or joint limits are invalid for {name}")
            low, high = self.joint_limits[name]
            if not low <= value <= high:
                raise ValueError(
                    f"IK solution for {name} exceeds joint limits: "
                    f"target_rad={value:.4f} limits_rad=[{low:.4f}, {high:.4f}]"
                )
            if abs(value - current[name]) > max_delta_rad:
                delta = abs(value - current[name])
                raise ValueError(
                    f"IK solution for {name} exceeds per-step joint limit: "
                    f"delta_rad={delta:.4f} limit_rad={max_delta_rad:.4f}"
                )
            result[name] = value
        actual = self.forward({**current, **result})
        pos_error = np.linalg.norm(actual[:3, 3] - target[:3, 3])
        rot_delta = actual[:3, :3].T @ target[:3, :3]
        rot_error = np.arccos(np.clip((np.trace(rot_delta) - 1.0) / 2.0, -1.0, 1.0))
        if pos_error > IK_POSITION_RESIDUAL_TOLERANCE_M or rot_error > IK_ROTATION_RESIDUAL_TOLERANCE_RAD:
            raise RuntimeError(
                "IK residual too large: "
                f"position={pos_error:.4f}m (limit={IK_POSITION_RESIDUAL_TOLERANCE_M:.4f}m) "
                f"rotation={rot_error:.4f}rad (limit={IK_ROTATION_RESIDUAL_TOLERANCE_RAD:.4f}rad)"
            )
        return result


class URDFFrameFK:
    """Forward kinematics from a robot base to any URDF frame on its chain."""

    def __init__(self, urdf_path: str, base_link: str, frame_link: str):
        from ikpy.chain import Chain

        elements = urdf_chain_elements(urdf_path, base_link, frame_link)
        self.chain = Chain.from_urdf_file(urdf_path, base_elements=elements)
        self.link_names = [getattr(link, "name", "") for link in self.chain.links]
        tip_joint_name = elements[-2]
        if tip_joint_name not in self.link_names:
            raise ValueError(f"FK chain does not include the joint leading to {frame_link}")
        tip_index = self.link_names.index(tip_joint_name)
        self.chain.links = self.chain.links[:tip_index + 1]
        self.chain._length = sum(link.length for link in self.chain.links)
        self.link_names = self.link_names[:tip_index + 1]
        root = ET.parse(urdf_path).getroot()
        joint_types = {joint.get("name"): joint.get("type") for joint in root.findall("joint")}
        chain_joints = {name for name in self.link_names[1:] if name in joint_types}
        self.required_joints = {
            name for name in chain_joints if joint_types[name] != "fixed"
        }

    def forward(self, joint_values: dict[str, float]) -> np.ndarray:
        missing = self.required_joints - joint_values.keys()
        if missing:
            raise ValueError("missing FK feedback for: " + ", ".join(sorted(missing)))
        seed = np.zeros(len(self.chain.links), dtype=np.float64)
        for index, name in enumerate(self.link_names):
            if name in joint_values:
                seed[index] = joint_values[name]
        T = np.asarray(self.chain.forward_kinematics(seed), dtype=np.float64)
        if T.shape != (4, 4) or not np.isfinite(T).all():
            raise RuntimeError("URDF frame forward kinematics returned invalid pose")
        return T
