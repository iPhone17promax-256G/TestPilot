---
name: web-testing
description: 先观察页面，再用可编辑步骤设计具有自动等待的 Web 回归测试。
---

默认使用可视化 steps，用户不提格式也一样。先 steps_capabilities(kind) 读取当前支持的动作、字段、示例。cases_save_revision 的 steps 必填，字段平铺；平台自动编译成原生执行代码并生成逐步映射。复杂的局部逻辑可用 python 步骤，但必须填写 reason，保留正常操作和断言步骤。整份脚本仅通过 cases_save_script：用户本轮明确要求时引用原话，或真实存在 被测试实际使用的 pytest fixture、未覆盖 JMeter 组件的缺口；后端校验并记录理由。不能因为写代码更方便，或因为循环、分支、变量、多标签页、CSV 而回退。只讨论或只编写时不要 runs_start。

先 browser_explore 观察真实页面。使用 role/label/testid 或稳定 CSS；iframe 填 frame，重复元素填 nth。元素编号、截图坐标不能作永久定位器。
goto → if(source=element,condition=visible,timeout=1500) → click可选提示 → end → fill → click → assert。分支有界等待，不用瞬时 count/is_visible 判断页面是否准备好。正常操作和 expect 断言由编译器自动等待与重试，避免固定 sleep。
支持多标签页 open_tab/click_popup/switch_tab/close_tab，初始 main；下载用 download(value=报告文件名)，上传用 upload(value=辅助文件名)，scroll/drag、dialog、reload/back。复杂网络拦截等用局部 python+reason，直接操作 page，不替换其他步骤。
先保存再独立运行，查看截图、Trace、日志。业务预期来自需求；不要为通过而改断言。探索本身不是回归测试。

网络证据：browser_explore / Web runs_get 返回 network.id。先 network_list 查请求摘要和失败请求，再 network_read(section=metadata/request_body/response_body) 分页取详情。默认接口记录；正文不足可 network_capture_set(level=detailed,reason=...) 后重新操作，只影响本轮后续浏览器会话。用户关闭时不得重开。流量不自动作为业务预期，不主动重放有副作用的请求。
