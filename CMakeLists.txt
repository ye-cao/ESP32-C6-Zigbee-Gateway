cmake_minimum_required(VERSION 3.16)

set(IDF_TARGET "esp32c6")

# ★★★ 固定版本号，别再让 IDF 去跑 `git describe`。
#
#   不设这一项时，IDF 的 project.cmake 会把**项目仓库的** .git/HEAD 和
#   .git/refs/heads/master 写进 build.ninja 的 RERUN_CMAKE 依赖里（为了生成
#   "v1.5.4-21-g6f7ba4f-dirty" 这种版本串）。后果：**每 git commit 一次，
#   下一次构建就要完整重跑 CMake 配置**，实测 185 s configure + 262 s
#   generate ≈ 7.5 分钟，之后才编那一两个改动过的文件 —— 改一行代码等八
#   分钟，非常难受（2026-11-07 用户提出"每次增量编译都这么慢？"）。
#
#   写完这一项后 build.ninja 不再依赖 git refs，只有真的改了 CMakeLists /
#   sdkconfig 才会重新配置，日常改 .c/.h 就是几秒钟。
#
#   版本号的唯一权威是 host/addon/esp32c6_gateway/config.json（用户界面上
#   看到的那个），这里跟着它走。发版时两处一起改。
set(PROJECT_VER "1.5.5")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(zigbee-vital-sensor)
