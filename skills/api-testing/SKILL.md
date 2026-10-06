---
name: api-testing
description: 用请求、提取、断言、参数组合设计接口回归测试。
---

默认使用可视化 steps，用户不提格式也一样。先 steps_capabilities(kind) 读取当前支持的动作、字段、示例。cases_save_revision 的 steps 必填，字段平铺；平台自动编译成原生执行代码并生成逐步映射。复杂的局部逻辑可用 python 步骤，但必须填写 reason，保留正常操作和断言步骤。整份脚本仅通过 cases_save_script：用户本轮明确要求时引用原话，或真实存在 被测试实际使用的 pytest fixture、未覆盖 JMeter 组件的缺口；后端校验并记录理由。不能因为写代码更方便，或因为循环、分支、变量、多标签页、CSV 而回退。只讨论或只编写时不要 runs_start。

先读接口资料。典型步骤是 request → assert(source=status) → extract → 后续 request(headers={Authorization:"Bearer {{token}}"}) → 业务 assert；客户端自动保持 Cookie。JSON 查询支持 path/jmespath/jsonpath，正文正则用 query=regex。缺失字段或提取失败不能当作成功。
request 支持 params、JSON/form/text/multipart、重定向、阶段超时；multipart 的 uploads 为字段名→辅助文件名，其他表单字段填 body，内容随 files 保存。响应时间上限用 assert(source=response_time)，不是请求 timeout。
foreach(items=[参数对象],variable="data") 对数据组合逐项测试；可以用 extract(source=variable,expression="data.xxx") 读取嵌套字段再引用。if/else/end 实现明确的条件；不要据当前结果放宽预期。局部计算用 python+reason 操作 rt.variables。
正确账号必须来自资料或环境凭据；缺失时说明限制，不编造密码。测试数据清理可写为正常请求步骤；需要失败也清理的自定义 pytest fixture 属于真实生命周期缺口，必须说明后再用例外工具。
