# HumanEgo 本地控制台

这是运行在机器人本机的进程控制台。页面只监听 `127.0.0.1`，启动后会按顺序运行 Feetech、CAN、Damiao、HumanEgo ROS 适配器和 Feetech 状态监视。HumanEgo 客户端不会自动启动，需在客户端卡片中选择模式并手动启动。

## 启动

```bash
cd /home/nvidia/ROS/ego_ws/src/humanego_control_center
./start_console.sh
```


脚本默认监听独立的 `8876` 端口，不会与其他控制台的 `8765` 端口冲突。脚本会通过系统 sudo 验证后启动 root 管理器，并在终端打印带访问令牌的本机页面地址。也可用 `HUMANEGO_PORT=8877 ./start_console.sh` 更换端口，或用 `TELEOP_NO_BROWSER=1` 禁止自动打开浏览器。使用 SSH 时将端口转发到本机浏览器，例如 `ssh -L 8876:127.0.0.1:8876 用户名@机器人IP`。

自动启动顺序为：

1. `/home/nvidia/ROS/humanoid_control_ws/src/feetech.bash`
2. `/home/nvidia/ROS/humanoid_control_ws/src/can.bash`（root）
3. `/home/nvidia/ROS/humanoid_control_ws/src/damiao.bash`（root）
4. `roslaunch humanego_ros_adapter adapter.launch`
5. `rostopic echo /feedback_feetech_joint_states`

ROS adapter 使用 `/opt/ros/noetic/setup.bash` 和 `/home/nvidia/ROS/ego_ws/devel/setup.bash`。客户端会在 `record_only` 与 `execute` 间选择；默认是 `record_only`，不会向 `/command_joint_states` 发布命令。选择 `execute` 会启用真实命令发布，应在确认机器人、标定、限位和急停状态后使用。

Damiao 启动后，控制台会等待 `/damiao_motor_node` 在 `/feedback_damiao_joint_states` 连续发布两条有效的 8 关节反馈，最多等待 15 秒（整体启动检查上限 20 秒）。反馈未通过时自动启动序列会暂停，Damiao 进程仍保留在任务卡片中，可从页面停止并查看检查日志。

## 控制与停止

- `一键启动` 只启动上述五个自动任务，不启动 HumanEgo 客户端。
- 客户端卡片提供模式选择和独立启动按钮；停止后可再次选择模式并启动。
- `全部重启` 会先逆序停止全部任务，再重启五个自动任务；客户端保持停止。
- `全部停止`、控制台退出和单项停止会先停止客户端，再逆序停止自动任务。每项先收到 SIGINT，超时后才升级为 TERM/KILL。
- 单项终端显示标准输出和错误输出，可查看 PID、退出码和运行时间。

控制台任务命令和清理特征集中在 `tasks.json`。root 任务由 root 管理器执行，普通用户任务由操作用户运行。操作用户通过 `SUDO_USER` 获取，直接以 root 启动时须显式设置 `TELEOP_OPERATOR_USER`。

## 验证

如已单独准备客户端测试文件，可运行：

```bash
cd /home/nvidia/ROS/ego_ws/src/humanego_client
PYTHONPATH=src python -m unittest discover -s tests -v
```

只启动控制台而不启动自动任务：

```bash
./start_console.sh --no-auto-start
```
