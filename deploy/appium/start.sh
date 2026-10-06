#!/usr/bin/env bash
set -euo pipefail
if [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then echo "Android 模拟器需要可访问的 /dev/kvm" >&2; exit 1; fi
mkdir -p /root/.android/avd
if [[ ! -f /root/.android/avd/aiqa-android.ini ]]; then
 echo no | avdmanager create avd --name aiqa-android --package 'system-images;android-30;default;x86_64' --device pixel
fi
# Own the AVD volume before clearing lock files left by an abrupt container/WSL exit.
exec 9>/root/.android/avd/aiqa-node.lock
flock -n 9 || { echo "AVD volume is already owned by another AIQA node" >&2; exit 1; }
avd_dir=/root/.android/avd/aiqa-android.avd
[[ "$(realpath "$avd_dir")" == "$avd_dir" ]] || { echo "Unexpected AVD directory" >&2; exit 1; }
rm -rf -- "$avd_dir/hardware-qemu.ini.lock" "$avd_dir/multiinstance.lock"
adb start-server
emulator -avd aiqa-android -port 5554 -no-window -no-audio -no-boot-anim -no-snapshot -gpu swiftshader_indirect -memory 1536 -cores 2 -camera-back none -camera-front none > /tmp/emulator.log 2>&1 &
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
# Treat any critical process exiting as node failure; restart policy then restores the whole node.
set +e
wait -n "$emulator_pid" "$appium_pid" "$node_pid"
exit_code=$?
set -e
echo "Android node process exited (status=$exit_code); restarting node" >&2
exit 1
