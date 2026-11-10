cmake_minimum_required(VERSION 3.0.2)
project(humanego_control_center)

find_package(catkin REQUIRED)
catkin_package()

install(PROGRAMS server.py start_console.sh check_damiao_feedback.py
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
)
install(DIRECTORY web/
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}/web
)
install(FILES tasks.json
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
)
