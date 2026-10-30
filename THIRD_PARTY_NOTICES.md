# 第三方组件说明

TestPilot 自有源码采用 [MIT License](LICENSE)。第三方库、基础镜像、测试工具和示例应用保留各自的版权与许可证，不因 TestPilot 的许可证而改变。

- Python 与 JavaScript 依赖分别列在 `pyproject.toml`、`requirements.lock`、`web/package.json` 和 `web/package-lock.json`；安装后的依赖包保留上游许可文件。
- MySQL、Redis、Qdrant、Chromium、JMeter、Appium、UiAutomator2 和基础操作系统镜像是独立组件，按各自上游许可使用；构建文件中的镜像标签和版本用于标识这些组件。
- Android 构建脚本会下载 Android SDK、模拟器及系统镜像。构建者须先阅读并接受适用的 [Android SDK 条款](https://developer.android.com/studio/terms)；这些组件不适用 TestPilot 的 MIT 许可。
- Android 演示使用 [Appium ApiDemos](https://github.com/appium/android-apidemos)，其许可见[上游 LICENSE](https://github.com/appium/android-apidemos/blob/master/LICENSE)。演示 APK 由 Android 构建脚本从上游下载，不作为本仓库源码文件上传。
- README 中的 Mattermost 截图和示例用于展示对自建被测系统的测试流程。Mattermost 不是 TestPilot 的组成部分，也不随本仓库分发；相关商标及软件权利属于其各自权利人。

发布包含第三方二进制组件的成品镜像或离线包时，应保留相关组件的许可与声明，并遵守其再分发条件。本次 GitHub 发布提供源码，不上传本机现有镜像、SDK、用户资料或执行产物。
