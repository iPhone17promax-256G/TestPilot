---
name: app-testing
description: 先观察真实 Android 设备，再用步骤编写和执行 APP 回归测试。
---

默认使用可视化 steps，用户不提格式也一样。先 steps_capabilities(kind) 读取当前支持的动作、字段、示例。cases_save_revision 的 steps 必填，字段平铺；平台自动编译成原生执行代码并生成逐步映射。复杂的局部逻辑可用 python 步骤，但必须填写 reason，保留正常操作和断言步骤。整份脚本仅通过 cases_save_script：用户本轮明确要求时引用原话，或真实存在 被测试实际使用的 pytest fixture、未覆盖 JMeter 组件的缺口；后端校验并记录理由。不能因为写代码更方便，或因为循环、分支、变量、多标签页、CSV 而回退。只讨论或只编写时不要 runs_start。

先 applications_list/devices_list/devices_check，mobile_action open/observe 取得真实截图和 XML；按需 click/input/back/swipe 探索。正式运行前 close 释放设备。
保存 kind=app 的可视化步骤，execution_config={app_id,device_id,reset}。定位优先 accessibility id/id，XPath 必要时使用；步骤提供 click/fill/clear/check/uncheck/assert_checked、等待、文本/属性/可用状态断言、extract、press、back/swipe/screenshot、条件和循环。
平台管理 driver；不定义 driver fixture 或 webdriver.Remote，不写死节点地址。隐式等待为0，编译器使用 WebDriverWait 显式等待。Android press 支持 ENTER/BACK/HOME/DELETE/TAB 或 keycode。
运行后查看真实 PNG/XML/Appium/logcat 日志；设备连接错误与断言失败分开。当前支持 Android，不声称已实现 iOS/WebView 专项适配。
