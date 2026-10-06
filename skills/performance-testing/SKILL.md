---
name: performance-testing
description: 通过可视化请求、CSV 和控制器设计 JMeter 测试，验证真实指标。
---

默认使用可视化 steps，用户不提格式也一样。先 steps_capabilities(kind) 读取当前支持的动作、字段、示例。cases_save_revision 的 steps 必填，字段平铺；平台自动编译成原生执行代码并生成逐步映射。复杂的局部逻辑可用 python 步骤，但必须填写 reason，保留正常操作和断言步骤。整份脚本仅通过 cases_save_script：用户本轮明确要求时引用原话，或真实存在 被测试实际使用的 pytest fixture、未覆盖 JMeter 组件的缺口；后端校验并记录理由。不能因为写代码更方便，或因为循环、分支、变量、多标签页、CSV 而回退。只讨论或只编写时不要 runs_start。

先读资料确定请求、鉴权、负载和验收目标。直接保存 kind=performance 的 steps 与 execution_config，不走先生成 JMX 再保存 code 的旧流程。
request 支持 params、JSON/form/text、超时、重定向、expected_status、body_pattern、extract_json、max_response_ms。变量 {{token}} 编译为 JMeter 变量。账号数据用 csv(value="accounts.csv",columns="login_id,password")，文本随 files 保存；可选首行、分隔符、循环读、EOF停止和共享模式。
once/end 为每用户仅一次（通常登录），repeat(count)/end 为循环，transaction/end 为事务；可嵌套，仍可界面编辑和查看 JMX。事务统计不与 HTTP 样本混算。只支持标准 ThreadGroup，阶段通过 execution_config.stages 配置。
负载字段 users、duration_seconds、ramp_seconds、think_ms、warmup_seconds、p95_ms、max_error_rate、min_requests、stages。先单用户3秒覆盖验证，之后按需求施压，查询真实 metrics、failures、thresholds 和报告。用户数不等于 RPS，退出正常不代表达标；未设阈值不能说性能达标。
本机争用资源只适合固定条件演示，不虚构服务端瓶颈。未覆盖的吞吐控制器、第三方插件等才走 jmeter_component 回退；现有 CSV/循环/事务不能作为整份脚本的理由。
