from __future__ import annotations

import time

import cv2
import numpy as np


class USBStereoCamera:
    """Read a side-by-side UVC stream, rectify both views, and return RGB frames."""

    def __init__(self, config: dict, calibration: dict, capture: cv2.VideoCapture | None = None):
        self.config = config
        self.width = int(config["output_width"])
        self.height = int(config["output_height"])
        self.capture_width = int(config["capture_width"])
        self.capture_height = int(config["capture_height"])
        if self.capture_width % 2 != 0:
            raise ValueError("capture_width must be even for a horizontal side-by-side stream")
        self.capture_eye_width = self.capture_width // 2
        if self.capture_eye_width <= 0 or self.capture_height <= 0:
            raise ValueError("capture dimensions must be positive")
        if int(calibration["image_width"]) != self.width or int(calibration["image_height"]) != self.height:
            raise ValueError("stereo calibration resolution must match rectified output resolution")
        size = (self.width, self.height)
        K1 = np.asarray(calibration["camera_matrix_left"], dtype=np.float64)
        D1 = np.asarray(calibration["distortion_left"], dtype=np.float64)
        K2 = np.asarray(calibration["camera_matrix_right"], dtype=np.float64)
        D2 = np.asarray(calibration["distortion_right"], dtype=np.float64)
        R = np.asarray(calibration["rotation_left_to_right"], dtype=np.float64)
        T = np.asarray(calibration["translation_left_to_right_m"], dtype=np.float64).reshape(3, 1)
        R1, R2, P1, P2, _, _, _ = cv2.stereoRectify(K1, D1, K2, D2, size, R, T, flags=cv2.CALIB_ZERO_DISPARITY, alpha=0)
        self.left_rectification_rotation = R1
        self.left_map = cv2.initUndistortRectifyMap(K1, D1, R1, P1, size, cv2.CV_32FC1)
        self.right_map = cv2.initUndistortRectifyMap(K2, D2, R2, P2, size, cv2.CV_32FC1)
        self.capture = capture
        if self.capture is None:
            backend = cv2.CAP_V4L2 if config.get("backend", "v4l2") == "v4l2" else cv2.CAP_ANY
            self.capture = cv2.VideoCapture(config["device"], backend)
            if self.capture.isOpened():
                self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(config["capture_width"]))
                self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(config["capture_height"]))
                self.capture.set(cv2.CAP_PROP_FPS, int(config["capture_fps"]))
                fourcc = config.get("fourcc")
                if fourcc:
                    self.capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        if not self.capture.isOpened():
            raise RuntimeError(f"unable to open USB camera {config['device']}")

    def read(self) -> dict:
        ok, frame = self.capture.read()
        captured_at = time.monotonic()
        if not ok or frame is None:
            raise RuntimeError("USB camera frame read failed")
        expected_shape = (self.capture_height, self.capture_width, 3)
        if frame.shape[:2] != expected_shape[:2]:
            raise RuntimeError(
                f"unexpected side-by-side frame shape {frame.shape}; expected {expected_shape}"
            )
        left_first = bool(self.config.get("left_first", True))
        first, second = np.split(frame, 2, axis=1)
        left_bgr, right_bgr = (first, second) if left_first else (second, first)
        # Calibrations are defined at the transmitted 640x480 eye resolution.
        # Downsample before rectification so the maps and camera matrices share a frame.
        resize_size = (self.width, self.height)
        left_bgr = cv2.resize(left_bgr, resize_size, interpolation=cv2.INTER_AREA)
        right_bgr = cv2.resize(right_bgr, resize_size, interpolation=cv2.INTER_AREA)
        left = cv2.remap(left_bgr, *self.left_map, interpolation=cv2.INTER_LINEAR)
        right = cv2.remap(right_bgr, *self.right_map, interpolation=cv2.INTER_LINEAR)
        return {
            "left": cv2.cvtColor(left, cv2.COLOR_BGR2RGB),
            "right": cv2.cvtColor(right, cv2.COLOR_BGR2RGB),
            "captured_at": captured_at,
        }

    def close(self) -> None:
        if self.capture is not None:
            self.capture.release()
