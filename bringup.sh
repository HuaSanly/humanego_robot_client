cd ~/ROS/ego_ws
source /opt/ros/noetic/setup.bash
source ~/ROS/ego_ws/devel/setup.bash

roslaunch humanego_ros_adapter adapter.launch


roslaunch humanego_ros_adapter camera_stereo.launch

rosrun camera_calibration cameracalibrator.py \
  --size 11x8 \
  --square 0.020 \
  --no-service-check \
  left:=/stereo/left/image_raw \
  right:=/stereo/right/image_raw \
  left_camera:=/stereo/left \
  right_camera:=/stereo/right