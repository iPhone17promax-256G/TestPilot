#!/usr/bin/env bash
set -euo pipefail
if [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then echo "Android 模拟器需要可访问的 /dev/kvm" >&2; exit 1; fi
mkdir -p /root/.android/avd
if [[ ! -f /root/.android/avd/testpilot-android.ini ]]; then
 echo no | avdmanager create avd --name testpilot-android --package 'system-images;android-30;default;x86_64' --device pixel
fi
# 先通过 flock 独占模拟器 AVD 数据卷，再清理异常退出遗留的启动锁；否则可能误删另一节点正在使用的模拟器锁文件。
exec 9>/root/.android/avd/testpilot-node.lock
flock -n 9 || { echo "AVD volume is already owned by another TestPilot node" >&2; exit 1; }
avd_dir=/root/.android/avd/testpilot-android.avd
[[ "$(realpath "$avd_dir")" == "$avd_dir" ]] || { echo "Unexpected AVD directory" >&2; exit 1; }
rm -rf -- "$avd_dir/hardware-qemu.ini.lock" "$avd_dir/multiinstance.lock"
adb start-server
emulator -avd testpilot-android -port 5554 -no-window -no-audio -no-boot-anim -no-snapshot -gpu swiftshader_indirect -memory 1536 -cores 2 -camera-back none -camera-front none > /tmp/emulator.log 2>&1 &
emulator_pid=$!
cleanup(){ trap - EXIT TERM INT; kill "$emulator_pid" "${appium_pid:-}" "${node_pid:-}" 2>/dev/null || true; wait "$emulator_pid" "${appium_pid:-}" "${node_pid:-}" 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
for attempt in $(seq 1 180); do
 if ! kill -0 "$emulator_pid" 2>/dev/null; then tail -50 /tmp/emulator.log >&2; exit 1; fi
 if [[ "$(adb -s emulator-5554 shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == 1 ]]; then break; fi
 sleep 2
done
[[ "$(adb -s emulator-5554 shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == 1 ]] || { echo '模拟器启动超时'; exit 1; }
for setting in window_animation_scale transition_animation_scale animator_duration_scale; do adb -s emulator-5554 shell settings put global "$setting" 0; done
adb -s emulator-5554 shell input keyevent 82
adb -s emulator-5554 install -r /opt/ApiDemos.apk
python3 /opt/device-node.py & node_pid=$!
appium --address 0.0.0.0 --port 4723 --log-timestamp --log-no-colors --log /tmp/appium.log & appium_pid=$!
# emulator、Appium 或设备代理任一进程退出，都让本节点失败退出；Compose 重启策略随后重启整套节点，避免留下只有部分进程存活的假在线设备。
set +e
wait -n "$emulator_pid" "$appium_pid" "$node_pid"
exit_code=$?
set -e
echo "Android node process exited (status=$exit_code); restarting node" >&2
exit 1
