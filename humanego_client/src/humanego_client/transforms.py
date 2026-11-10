from __future__ import annotations

import numpy as np


def validate_transform(T: np.ndarray, name: str = "transform") -> np.ndarray:
    T = np.asarray(T, dtype=np.float64)
    if T.shape != (4, 4) or not np.isfinite(T).all():
        raise ValueError(f"{name} must be a finite 4x4 matrix")
    if not np.allclose(T[3], [0, 0, 0, 1], atol=1e-5):
        raise ValueError(f"{name} has invalid homogeneous row")
    R = T[:3, :3]
    if not np.allclose(R.T @ R, np.eye(3), atol=2e-3) or not np.isclose(np.linalg.det(R), 1.0, atol=2e-3):
        raise ValueError(f"{name} rotation is not a proper rotation")
    return T


def rotation6d_from_matrix(R: np.ndarray) -> np.ndarray:
    return np.asarray(R, dtype=np.float32)[:, :2].reshape(6, order="C")


def rectified_optical_from_base(
    T_base_from_camera_link: np.ndarray,
    T_optical_from_camera_link: np.ndarray,
    R_left_rectification: np.ndarray,
) -> np.ndarray:
    T_optical_from_camera_link = validate_transform(
        T_optical_from_camera_link, "T_optical_from_camera_link"
    )
    R = np.asarray(R_left_rectification, dtype=np.float64)
    if R.shape != (3, 3) or not np.isfinite(R).all():
        raise ValueError("left rectification rotation must be a finite 3x3 matrix")
    T_rectified_from_optical = np.eye(4)
    T_rectified_from_optical[:3, :3] = R
    return validate_transform(T_rectified_from_optical) @ T_optical_from_camera_link @ np.linalg.inv(
        validate_transform(T_base_from_camera_link, "T_base_from_camera_link")
    )


def matrix_from_rotation6d(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64).reshape(6)
    first = values[[0, 2, 4]]
    second = values[[1, 3, 5]]
    norm = np.linalg.norm(first)
    if norm < 1e-8:
        raise ValueError("rotation6d first axis is degenerate")
    first /= norm
    second -= first * np.dot(first, second)
    norm = np.linalg.norm(second)
    if norm < 1e-8:
        raise ValueError("rotation6d axes are degenerate")
    second /= norm
    third = np.cross(first, second)
    return np.column_stack((first, second, third))


def ee_state_in_camera(T_camera_from_base: np.ndarray, T_base_from_hand: np.ndarray, gripper: float) -> np.ndarray:
    T = validate_transform(T_camera_from_base) @ validate_transform(T_base_from_hand)
    if T[2, 3] <= 0:
        raise ValueError(f"end-effector has nonpositive rectified optical depth: {T[2, 3]:.4f} m")
    if not np.isfinite(gripper) or not 0 <= gripper <= 1:
        raise ValueError("normalized gripper state must be in [0, 1]")
    return np.concatenate((T[:3, 3], rotation6d_from_matrix(T[:3, :3]), [gripper])).astype(np.float32)


def hand_target_in_base(T_camera_from_base: np.ndarray, action: np.ndarray) -> np.ndarray:
    action = np.asarray(action, dtype=np.float64)
    if action.shape != (10,) or not np.isfinite(action).all():
        raise ValueError("action must be finite with shape [10]")
    T_camera_from_ee = np.eye(4)
    T_camera_from_ee[:3, :3] = matrix_from_rotation6d(action[3:9])
    T_camera_from_ee[:3, 3] = action[:3]
    return np.linalg.inv(validate_transform(T_camera_from_base)) @ T_camera_from_ee


def urdf_ee_target_from_hand(T_base_from_hand: np.ndarray, T_urdf_ee_from_hand: np.ndarray) -> np.ndarray:
    return validate_transform(T_base_from_hand) @ np.linalg.inv(
        validate_transform(T_urdf_ee_from_hand, "T_urdf_ee_from_hand")
    )
