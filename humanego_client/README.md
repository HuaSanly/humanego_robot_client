# HumanEgo local client

This package captures a USB stereo camera directly, rectifies the side-by-side UVC stream, reads robot state through a local ZeroMQ bridge, and uses the official LeRobot remote inference client. It does not use ROS camera topics.

## Environment

Create the isolated environment from this directory:

    conda env create -f environment.yml
    conda activate humanego

The environment pins Python 3.12.15, PyTorch 2.7.1, torchvision 0.22.1, and the LeRobot revision validated by the server branch. This Jetson client uses CPU PyTorch for remote request serialization and local FK/IK; the inference model and GPU work remain on the server. Create the environment with `conda env create -f environment.yml` and activate it with `conda activate humanego`.

## Configuration

1. Set config/camera.yaml to the UVC device and packed side-by-side mode. The reference USB camera is read at `5184x1944` (`2592x1944` per eye), each eye is downsampled to `640x480`, then rectified and sent as RGB. The device advertises `5184x1944`; `5194x1944` is not a supported mode.
2. Fill config/calibration/stereo.yaml with measured intrinsics, distortion, relative rotation and translation in metres. Set calibration_validated: true only after checking rectification.
3. `config/calibration/hand_eye.yaml` defines the axis conversion from the URDF `EYEL_Link` to the raw left optical frame. The client combines it with live URDF FK from the head feedback and OpenCV's left rectification rotation `R1` for every observation. Head feedback is used for camera localization only; head joints are never action targets, so normal head motor noise does not stop the client. The transform currently assumes the `EYEL_Link` origin is at the left optical center; verify that translation against the camera CAD or a hand-eye calibration.
4. `config/client.yaml` is preconfigured for `tcp/192.168.5.16:7447`, deployment `humanego-pick-cube-anchor`, and the exported HumanEgo semantics string. Images are JPEG-encoded at quality 85 before transport and decoded by the remote inference protocol to the same RGB feature shape. `humanego_brx` is the server `robot_type`, not the Zenoh deployment name. A local `server.yaml` is optional when these three values are present.
5. Start the ROS-side adapter described below. Leave control.mode as record_only until the robot adapter, IK, limits, calibrations and emergency stop have been checked. Change to execute only for supervised low-speed commissioning.

Start the ROS adapter first with `roslaunch humanego_ros_adapter adapter.launch`, then run `launch/humanego_client.sh` from a second terminal. Do not launch `camera_stereo.launch`: the client process owns `/dev/video0` and reads the packed USB stream directly. Missing calibration values deliberately prevent startup.

## ROS bridge protocol

The ROS-side process runs in the existing ROS Noetic environment. It subscribes only to the Damiao arm and Feetech head/arm feedback topics, then publishes the complete motor vector to `/command_joint_states`; it does not perform FK or IK and does not publish end-effector targets. The HumanEgo Conda environment does not import rospy. The adapter listens on the configured ZeroMQ REP endpoint and accepts JSON requests:

    {"protocol":"humanego_ros_bridge_v1","id":"...","sequence":1,"operation":"get_state"}

It replies with the same id, ok: true, and feedback containing `captured_at_monotonic` plus `damiao[8]` and `feetech[10]` radian arrays. Timestamps use the host monotonic clock. For `record_command` and `publish_command`, requests contain the complete 21-position motor vector in the established order: three waist positions fixed at zero, eight Damiao positions, then ten Feetech positions. Only the explicit `execute` mode publishes a command.

The ROS topic names and fixed motor order were read from `robot_ego_client`; its implementation was not copied. The HumanEgo client owns the robot URDF FK/IK and maps the solved right-arm joints into the motor vector before sending it to the ROS adapter.

## Client data contract

The client sends RGB uint8 arrays shaped [480,640,3] for each rectified eye and a hand state `[x,y,z,R00,R01,R10,R11,R20,R21,gripper]` in left rectified optical coordinates (X image-right, Y image-down, Z forward). The hand origin is the midpoint between the two fingertips at the nominal closed position. Hand +X points from JawBlock01 toward JawBlock02, +Y points from the gripper base toward the fingertips, and +Z completes a right-handed frame. The fixed `robot.T_urdf_ee_from_hand` transform in `config/client.yaml` converts between this model frame and the URDF `LinearclampinggripperJZ01_Link` frame. State is transformed into the hand frame before transmission; returned hand-frame targets are transformed back to the URDF end-effector frame before local IK. The server returns 50 absolute actions of the same pose layout plus continuous grasp. Positions are metres. Waist joints use the fixed zero pose for FK and commands. Each target is checked against Cartesian and joint limits, then assembled into the full motor vector.

## Checks

If test files are available locally, run them with:

    PYTHONPATH=src python -m unittest discover -s tests -v

These checks do not validate camera calibration, robot IK, server task success, or the real-robot safety envelope.
