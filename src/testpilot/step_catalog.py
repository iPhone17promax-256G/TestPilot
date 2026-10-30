# Agent 按需查询的步骤能力目录，与 Step 参数模型和编译器共用定义。
# 不把所有动作的细节都写进固定提示词；Agent 调 steps_capabilities 后再按实际类型编写。

"""Compact capability discovery instead of putting every scripting recipe in the prompt."""
from .steps import ACTIONS, Step

LABELS = {
"request":"发送 HTTP 请求", "extract":"提取变量", "assert":"检查响应", "goto":"打开页面", "click":"点击元素", "dblclick":"双击元素", "hover":"悬停元素", "fill":"输入内容", "press":"按键", "select":"选择选项", "check":"勾选", "uncheck":"取消勾选", "wait":"等待元素状态", "assert_visible":"检查可见状态", "assert_text":"检查元素文字", "assert_url":"检查页面地址", "assert_enabled":"检查可用状态", "assert_value":"检查输入框值", "assert_attribute":"检查元素属性", "assert_count":"检查元素数量", "assert_checked":"检查勾选状态", "upload":"上传辅助文件", "screenshot":"保存截图", "dialog":"处理下一个弹窗", "back":"返回", "swipe":"滑动", "set_variable":"设置变量", "sleep":"固定暂停", "python":"Python 自定义步骤", "if":"条件分支", "else":"否则分支", "repeat":"重复执行", "foreach":"遍历数据", "end":"结束控制块", "open_tab":"打开新标签页", "switch_tab":"切换标签页", "close_tab":"关闭标签页", "click_popup":"点击并接管新标签页", "download":"点击并下载文件", "reload":"刷新页面", "scroll":"滚动至元素", "drag":"拖拽元素", "clear":"清空输入", "csv":"CSV 数据源", "once":"每用户仅执行一次", "transaction":"事务统计"}
FIELDS = {
"request":["method","path","headers","params","body","body_type","follow_redirects","expected_status","body_pattern","assert_json","uploads","extract_json","max_response_ms"],
"extract":["source","expression","query","match_group","variable","attribute"],
"assert":["source","expression","query","match_group","operator","expected"],
"goto":["path"], "fill":["value"], "press":["value"], "select":["value"],
"wait":["state"], "assert_visible":["expected"], "assert_text":["value","text_exact"], "assert_url":["value"], "assert_enabled":["expected"], "assert_checked":["expected"], "assert_value":["value"], "assert_count":["expected"], "assert_attribute":["attribute","value"],
"upload":["value"], "screenshot":["value"], "dialog":["value"], "swipe":["direction"], "set_variable":["variable","expected"], "sleep":["duration_ms"], "python":["python","reason"],
"if":["source","expression","query","match_group","operator","expected","condition","value"], "repeat":["count","variable"], "foreach":["items","variable"],
"open_tab":["tab","path"], "switch_tab":["tab"], "close_tab":["tab"], "click_popup":["tab"], "download":["value"], "drag":["target_selector"],
"csv":["value","columns","delimiter","ignore_first_line","recycle","stop_thread","share_mode"],
}
LOCATORS=["by","selector","role","exact","frame","nth"]
EXAMPLES={
"api":[{"action":"request","name":"检查健康接口","path":"/api/v4/system/ping","expected_status":200},{"action":"assert","expression":"status","expected":"OK"}],
"web":[{"action":"goto","path":"/login"},{"action":"if","source":"element","by":"role","role":"link","selector":"View in Browser","condition":"visible","timeout":1500},{"action":"click","by":"role","role":"link","selector":"View in Browser"},{"action":"end"},{"action":"assert_visible","by":"label","selector":"Email or Username"}],
"app":[{"action":"fill","by":"id","selector":"真实观察到的资源ID","value":"tester"},{"action":"check","by":"accessibility id","selector":"真实复选框"},{"action":"assert_checked","by":"accessibility id","selector":"真实复选框"}],
"performance":[{"action":"csv","value":"accounts.csv","columns":"login_id,password","ignore_first_line":True},{"action":"once","name":"每用户登录一次"},{"action":"request","method":"POST","path":"/api/v4/users/login","body":{"login_id":"{{login_id}}","password":"{{password}}"},"extract_json":{"token":"$.token"},"expected_status":200},{"action":"end"},{"action":"repeat","count":2},{"action":"request","path":"/api/v4/system/ping","expected_status":200,"max_response_ms":1000},{"action":"end"}],
}

# 功能：生成四种测试类型的动作名称和中文标签清单，供界面及 Agent 发现可视化能力。
# 输入：无显式参数；ACTIONS 限定各类型可用动作，LABELS 提供显示标签。
# 输出/影响：返回类型到动作列表的字典；不包含运行结果、不注册 MCP 或执行动作，不读写数据库。
def catalog():
    return {kind:[dict(action=action,label=LABELS[action]) for action in LABELS if action in actions] for kind,actions in ACTIONS.items()}

# 根据测试类型返回可用动作、字段 schema、定位规则和例子。
# 这是告诉 Agent“如何填步骤”，不会创建用例，更不会替它决定业务预期。
# 功能：按测试类型提供编写步骤所需的动作参数 schema、定位字段、变量规则和例子。
# 输入：kind 为 api/web/app/performance；字段定义取自 Step 的 Pydantic schema，目录规则取自 FIELDS/LOCATORS。
# 输出/影响：返回可供工具交给 Agent 的能力说明字典；未知类型抛 ValueError，仅在内存组织 schema，不生成或执行用例。
def capabilities(kind):
    if kind not in ACTIONS: raise ValueError("未知测试类型")
    schema=Step.model_json_schema()["properties"]
    actions=[]
    for item in catalog()[kind]:
        action=item["action"]
        fields=list(FIELDS.get(action,[]))
        uses_locator = kind in {"web","app"} and action in {"click","dblclick","hover","fill","press","select","check","uncheck","wait","assert_visible","assert_text","assert_enabled","assert_value","assert_attribute","assert_count","assert_checked","extract","upload","click_popup","download","scroll","drag","clear","if"}
        if kind == "app" and action == "press": uses_locator = False
        if kind == "api" and action=="request":fields=[f for f in fields if f not in {"extract_json","max_response_ms"}]
        if kind == "performance" and action=="request":fields=[f for f in fields if f!="uploads"]
        if kind in {"web","app"} and action == "extract":fields=["variable","attribute"]
        if action == "if" and kind != "api":fields=[f for f in fields if f != "match_group"]
        if action == "if" and kind == "performance":fields=["source","expression","operator","expected"]
        field_schema={f:dict(schema[f]) for f in fields}
        if action == "if":
            field_schema["source"]["default"]="variable"
            if kind == "performance":
                field_schema["source"]["pattern"]="^variable$"
                field_schema["operator"]["pattern"]="^(eq|ne|contains|matches|gt|gte|lt|lte|exists|not_exists)$"
            elif kind in {"web","app"}:
                field_schema["source"]["pattern"]="^(variable|element)$"
                field_schema["query"]["pattern"]="^(path|jmespath|jsonpath)$"
        if kind == "performance" and action == "request":field_schema["body_type"]["pattern"]="^(json|form|text)$"
        actions.append(dict(**item,usesLocator=uses_locator,fields=field_schema))
    return dict(kind=kind,policy="默认保存可视化 steps。只填当前动作字段，所有字段平铺；code 不需要提交。",commonFields={k:schema[k] for k in ["id","name","action","enabled","timeout"]},locatorFields={f:schema[f] for f in LOCATORS} if kind in {"web","app"} else {},locatorNote="usesLocator=true 的动作使用 locatorFields；if 仅 source=element 时需要定位。字段全部平铺。",actions=actions,example=EXAMPLES[kind],controls="if/repeat/foreach/once/transaction 后必须有 end，可嵌套最多6层；else 仅在 if 内。API/Web/APP repeat 可选 variable 保存0起索引，foreach items=数组或{{变量}}。性能 if(source=variable,expression=变量名)/repeat/once/transaction 直接编译为 JMeter 控制器。",variables="{{token}}、{{item.name}}、{{item.ids[0]}}；API/Web/APP 凭据 {{env.TEST_PASSWORD}}。性能凭据用 CSV，文件内容随 files 保存。",pythonFallback="API/Web/APP 局部扩展用 python+reason；不要把整份用例藏成一个 python 步骤。",nativeFallback="完整脚本例外：用户明确要求，或真实 被测试实际使用的 pytest fixture、尚未覆盖的 JMeter 组件。使用 cases_save_script 提交 fallback；会验证并记录。")
