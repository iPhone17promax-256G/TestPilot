---
name: aiqa-guide
description: 可视化用例版本、真实运行、报告与工作区操作。
---

默认使用可视化 steps，用户不提格式也一样。先 steps_capabilities(kind) 读取当前支持的动作、字段、示例。cases_save_revision 的 steps 必填，字段平铺；平台自动编译成原生执行代码并生成逐步映射。复杂的局部逻辑可用 python 步骤，但必须填写 reason，保留正常操作和断言步骤。整份脚本仅通过 cases_save_script：用户本轮明确要求时引用原话，或真实存在 被测试实际使用的 pytest fixture、未覆盖 JMeter 组件的缺口；后端校验并记录理由。不能因为写代码更方便，或因为循环、分支、变量、多标签页、CSV 而回退。只讨论或只编写时不要 runs_start。

project_context 取得当前环境和资料。cases_save_revision(steps=...) 新建或追加不可变版本，不写 code。已有 case_id 保持同一用例身份；修复步骤继续保存 steps，保留历史报告。
先保存，按用户意图 runs_start，再 runs_get(wait_seconds=90) 查询真实结果。artifacts_read 分页读取证据；没有测试、全部跳过、导入错误都不能算通过。
workspace_patch/read/exec 只用于辅助资料和局部计算，不替代正式版本。Skill 是说明和可选资源，由 Agent 自己决定调用什么工具及是否执行辅助脚本。
