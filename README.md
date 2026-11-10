# HumanEgo 客户端与 ROS 适配器启动说明

本工作区包含两个部分：

- `humanego_client`：独立 `humanego` Conda 环境中的客户端，负责 USB 双目相机、图像校正、FK/IK、动作安全检查和远端推理通信。
- `humanego_ros_adapter`：ROS Noetic 适配器，只负责读取三个电机反馈话题，并通过 ZeroMQ 接收客户端命令。

客户端当前配置如下：

- HumanEgo 服务端：`tcp/192.168.5.16:7447`
- deployment：`humanego-pick-cube-anchor`
- semantics：`humanego.right-ee.left-rectified-optical.m.row-major-rotation6d.continuous-grasp.v1`
- robot type：`humanego_brx`
- ROS IPC：`tcp://127.0.0.1:5557`
- 相机设备：`/dev/video0`
- 相机输入：`5184x1944` 左右拼接图像
- 客户端输出：两路 `640x480 RGB uint8`
- 当前控制模式：`record_only`

服务端使用的 hand frame 定义为：原点在标称闭合位置两指尖接触点中点；`+X` 从 JawBlock01 指向 JawBlock02，`+Y` 从夹爪底座指向指尖，`+Z` 按右手系确定。客户端通过 `robot.T_urdf_ee_from_hand` 在这个 hand frame 与 URDF 夹爪本体 link 之间转换。

## 启动前检查

服务端必须已经启动 Zenoh/Remote Inference，并监听 `7447/TCP`。可以在客户端机器检查：

```bash
ping -c 1 192.168.5.16
nc -vz -w2 192.168.5.16 7447
```

机器人底层驱动也必须已经启动，并发布头部与双臂所需的两个反馈话题：

```text
/feedback_damiao_joint_states
/feedback_feetech_joint_states
```

确认话题有数据：

```bash
rostopic echo -n1 /feedback_damiao_joint_states
rostopic echo -n1 /feedback_feetech_joint_states
```

腰部电机不读取反馈；客户端按控制中心的零位约定，将腰部位置固定为 `0.0`。

## 启动 ROS 环境

在每个需要使用 ROS 命令的终端执行：

```bash
cd /home/nvidia/ROS/ego_ws
source /opt/ros/noetic/setup.bash
source devel/setup.bash
```

如果 ROS Master 尚未运行，启动：

```bash
roscore
```

然后在另一个终端启动 ROS 适配器：

```bash
cd /home/nvidia/ROS/ego_ws
source /opt/ros/noetic/setup.bash
source devel/setup.bash
roslaunch humanego_ros_adapter adapter.launch
```

适配器会监听本机 ZeroMQ 地址 `tcp://127.0.0.1:5557`。

ROS 适配器运行在 ROS Noetic 的系统 Python 3.8 中，因此需要系统侧 PyZMQ；`humanego` Conda 环境里的 `pyzmq` 不会自动提供给 ROS 节点。推荐安装系统包：

```bash
sudo apt update
sudo apt install python3-zmq
```

如果没有 sudo 权限，可安装到当前用户的 Python 目录：

```bash
/usr/bin/python3 -m pip install --user 'pyzmq==25.1.2'
```

确认 ROS 使用的 Python 能导入 ZeroMQ：

```bash
/usr/bin/python3 -c 'import zmq; print(zmq.__version__)'
```

## 创建或检查客户端环境

客户端环境定义在 `humanego_client/environment.yml`。如果环境尚未创建：

```bash
cd /home/nvidia/ROS/ego_ws/src/humanego_client
conda env create -f environment.yml
```

检查环境是否可用：

```bash
conda run -n humanego python - <<'PY'
import cv2
import ikpy
import numpy
import zmq
import yaml

print("humanego environment is ready")
print("OpenCV:", cv2.__version__)
PY
```

## 启动 HumanEgo 客户端

ROS 适配器启动后，在第三个终端运行：

```bash
/home/nvidia/ROS/ego_ws/src/humanego_client/launch/humanego_client.sh
```

启动脚本会自动：

1. 激活 `humanego` Conda 环境；
2. 设置客户端源码路径；
3. 读取 `config/client.yaml`；
4. 打开 `/dev/video0`；
5. 读取左右拼接图并缩放、校正为两路 `640x480 RGB`；
6. 从 ROS 适配器获取电机反馈；
7. 通过 FK 得到当前末端状态；
8. 连接 `192.168.5.16:7447` 获取动作块；
9. 在本地执行 IK、限位和动作安全检查。

不要启动 `humanego_ros_adapter/launch/camera_stereo.launch`。相机不通过 ROS 读取，而是由 HumanEgo 客户端独占读取 `/dev/video0`。

## 控制模式

默认配置是：

```yaml
control:
  mode: record_only
```

`record_only` 会完成远端推理、FK、IK 和动作安全检查，但不会向 `/command_joint_states` 发布电机目标，适合首次联调。

完成以下检查后，才允许启用真机控制：

- 服务端 admission 和动作块接收正常；
- 双目图像和校正结果正常；
- 三个反馈话题持续有数据；
- FK/IK 结果正确；
- 动作限位和急停逻辑验证完成；
- 夹爪映射和低速控制已验证。

确认后编辑 `humanego_client/config/client.yaml`：

```yaml
control:
  mode: execute
```

重新启动客户端后，适配器才会向 `/command_joint_states` 发布完整的 21 电机目标。

## 常见故障

### 服务端连接超时

确认服务端监听的是可被本机访问的地址，例如：

```text
tcp/0.0.0.0:7447
```

如果 `ping` 成功但 `nc -vz 192.168.5.16 7447` 失败，通常是服务端 Zenoh 未启动、端口不同或防火墙未放行。

### ROS 适配器获取不到反馈

检查适配器终端是否显示三个反馈源已更新，并确认机器人驱动发布的话题名与 `humanego_ros_adapter/config/adapter.yaml` 一致。

### 相机打开失败

确认设备存在且当前用户属于 `video` 组：

```bash
ls -l /dev/video0
groups
```

不要同时启动其他会占用 `/dev/video0` 的相机程序。

### 客户端标定或配置加载失败

检查以下文件：

```text
humanego_client/config/client.yaml
humanego_client/config/camera.yaml
humanego_client/config/calibration/stereo.yaml
humanego_client/config/calibration/hand_eye.yaml
```

如已单独准备本地测试文件，可运行：

```bash
cd /home/nvidia/ROS/ego_ws/src/humanego_client
PYTHONPATH=src python -m unittest discover -s tests -v
```

