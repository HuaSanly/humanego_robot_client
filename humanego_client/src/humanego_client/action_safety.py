from __future__ import annotations

import numpy as np

from .transforms import hand_target_in_base, validate_transform


class ActionSafety:
    def __init__(self, config: dict, T_camera_from_base: np.ndarray):
        self.config = config
        self.T_camera_from_base = validate_transform(T_camera_from_base, "T_camera_from_base")
        self.previous_position = None
        self.closed = False
        self.candidate = None
        self.candidate_count = 0

    def update_camera_transform(self, T_camera_from_base: np.ndarray):
        self.T_camera_from_base = validate_transform(T_camera_from_base, "T_camera_from_base")

    def filter(self, action: np.ndarray, current_T_base_from_ee: np.ndarray):
        if self.closed:
            raise RuntimeError("action safety is latched stopped")
        action = np.asarray(action, dtype=np.float64)
        if action.shape != (10,) or not np.isfinite(action).all():
            self.stop()
            raise ValueError("policy action must be finite with shape [10]")
        T_base_from_hand = hand_target_in_base(self.T_camera_from_base, action)
        pos = T_base_from_hand[:3, 3]
        low = np.asarray(self.config["min_position_m"], dtype=float)
        high = np.asarray(self.config["max_position_m"], dtype=float)
        if np.any(pos < low) or np.any(pos > high):
            self.stop()
            raise ValueError(
                "EE target is outside configured base-frame workspace: "
                f"target_m={np.round(pos, 4).tolist()} "
                f"bounds_m={np.round(low, 4).tolist()}..{np.round(high, 4).tolist()}"
            )
        current = validate_transform(current_T_base_from_ee, "current_T_base_from_ee")[:3, 3]
        max_step = float(self.config["max_position_step_m"])
        current_T = validate_transform(current_T_base_from_ee, "current_T_base_from_ee")
        if np.linalg.norm(pos - current) > max_step:
            self.stop()
            distance = float(np.linalg.norm(pos - current))
            raise ValueError(
                "EE target exceeds configured per-step position limit: "
                f"distance_m={distance:.4f} limit_m={max_step:.4f} "
                f"current_m={np.round(current, 4).tolist()} target_m={np.round(pos, 4).tolist()}"
            )
        relative_rotation = current_T[:3, :3].T @ T_base_from_hand[:3, :3]
        rotation_step = np.arccos(np.clip((np.trace(relative_rotation) - 1.0) / 2.0, -1.0, 1.0))
        if rotation_step > float(self.config["max_rotation_step_rad"]):
            self.stop()
            rotation_limit = float(self.config["max_rotation_step_rad"])
            raise ValueError(
                "EE target exceeds configured per-step rotation limit: "
                f"angle_rad={rotation_step:.4f} limit_rad={rotation_limit:.4f}"
            )
        grasp = float(action[9])
        if not 0 <= grasp <= 1:
            self.stop()
            raise ValueError("grasp output must be in [0,1]")
        close_th = float(self.config["grasp_close_threshold"])
        open_th = float(self.config["grasp_open_threshold"])
        if open_th >= close_th:
            raise ValueError("grasp open threshold must be below close threshold")
        desired = self.candidate if self.candidate is not None else False
        if grasp >= close_th:
            desired = True
        elif grasp <= open_th:
            desired = False
        if desired != self.candidate:
            self.candidate, self.candidate_count = desired, 1
        else:
            self.candidate_count += 1
        if self.candidate_count >= int(self.config["grasp_debounce_steps"]):
            self.gripper_closed = bool(self.candidate)
        else:
            self.gripper_closed = bool(getattr(self, "gripper_closed", False))
        self.previous_position = pos.copy()
        return T_base_from_hand, float(self.gripper_closed)

    def stop(self):
        self.closed = True
