from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> tuple[dict[str, Any], Path]:
    config_path = Path(path).resolve()
    with config_path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError("client config must be a YAML mapping")
    root = config_path.parent
    for key in ("camera_config", "calibration_dir"):
        value = Path(config[key])
        config[key] = value if value.is_absolute() else (root / value).resolve()
    return config, root


def load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def require_finite_matrix(value: Any, shape: tuple[int, ...], name: str):
    import numpy as np

    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != shape or not np.isfinite(matrix).all():
        raise ValueError(f"{name} must be a finite matrix with shape {shape}")
    return matrix


def load_calibrations(directory: str | Path):
    import numpy as np

    directory = Path(directory)
    stereo = load_yaml(directory / "stereo.yaml")
    hand_eye = load_yaml(directory / "hand_eye.yaml")
    required = (
        "camera_matrix_left", "distortion_left", "camera_matrix_right",
        "distortion_right", "rotation_left_to_right", "translation_left_to_right_m",
    )
    missing = [key for key in required if stereo.get(key) is None]
    if not stereo.get("calibration_validated", False):
        missing.append("calibration_validated=true")
    if missing:
        raise ValueError("stereo calibration is incomplete: " + ", ".join(missing))
    if not hand_eye.get("urdf_camera_frame"):
        raise ValueError("hand_eye calibration urdf_camera_frame is required")
    if hand_eye.get("T_optical_from_urdf_camera") is None:
        raise ValueError("hand_eye calibration T_optical_from_urdf_camera is required")
    for key in ("camera_matrix_left", "camera_matrix_right"):
        require_finite_matrix(stereo[key], (3, 3), key)
    for key in ("rotation_left_to_right",):
        rotation = require_finite_matrix(stereo[key], (3, 3), key)
        if not np.allclose(rotation.T @ rotation, np.eye(3), atol=2e-3) or not np.isclose(np.linalg.det(rotation), 1.0, atol=2e-3):
            raise ValueError("rotation_left_to_right must be a proper rotation")
    for key in ("distortion_left", "distortion_right"):
        distortion = np.asarray(stereo[key], dtype=np.float64)
        if distortion.size not in (4, 5, 8, 12, 14) or not np.isfinite(distortion).all():
            raise ValueError(f"{key} must contain 4, 5, 8, 12, or 14 finite coefficients")
    require_finite_matrix(stereo["translation_left_to_right_m"], (3,), "translation")
    hand_transform = require_finite_matrix(
        hand_eye["T_optical_from_urdf_camera"], (4, 4), "T_optical_from_urdf_camera"
    )
    if not np.allclose(hand_transform[3], [0, 0, 0, 1], atol=1e-6):
        raise ValueError("T_optical_from_urdf_camera must be an SE(3) homogeneous transform")
    hand_rotation = hand_transform[:3, :3]
    if not np.allclose(hand_rotation.T @ hand_rotation, np.eye(3), atol=2e-3) or not np.isclose(np.linalg.det(hand_rotation), 1.0, atol=2e-3):
        raise ValueError("T_optical_from_urdf_camera rotation must be a proper rotation")
    return stereo, hand_eye
