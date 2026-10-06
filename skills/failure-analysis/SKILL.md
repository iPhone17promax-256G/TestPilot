---
name: failure-analysis
description: 依据真实报告修复步骤并复验，不放宽业务预期。
---

默认使用可视化 steps，用户不提格式也一样。先 steps_capabilities(kind) 读取当前支持的动作、字段、示例。cases_save_revision 的 steps 必填，字段平铺；平台自动编译成原生执行代码并生成逐步映射。复杂的局部逻辑可用 python 步骤，但必须填写 reason，保留正常操作和断言步骤。整份脚本仅通过 cases_save_script：用户本轮明确要求时引用原话，或真实存在 被测试实际使用的 pytest fixture、未覆盖 JMeter 组件的缺口；后端校验并记录理由。不能因为写代码更方便，或因为循环、分支、变量、多标签页、CSV 而回退。只讨论或只编写时不要 runs_start。

先 runs_get，再按需读取执行日志、截图、Trace、XML。区分环境、编译/定位/步骤错误、业务失败。业务预期来自需求，不为通过放宽断言。
读取原用例 steps 与对应源码，只修正错误步骤，再保存新版本并真实复跑。局部无法表达的修复用有理由的 python 步骤，保留其余步骤。原生代码本身不能自动反向映射，但也不能默认把可视化版本改为整份脚本。
最终列出证据、用例/版本/运行 ID、实际结果和待确认项。

网络证据：browser_explore / Web runs_get 返回 network.id。先 network_list 查请求摘要和失败请求，再 network_read(section=metadata/request_body/response_body) 分页取详情。默认接口记录；正文不足可 network_capture_set(level=detailed,reason=...) 后重新操作，只影响本轮后续浏览器会话。用户关闭时不得重开。流量不自动作为业务预期，不主动重放有副作用的请求。
