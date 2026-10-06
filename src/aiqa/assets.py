# 四类用例共用的资产校验与版本摘要。
# 资产包含源码、辅助文件和执行配置；不只是编辑框里的那段 Python。

"""Validated versioned assets shared by every execution engine."""
import ast, hashlib, json
from pathlib import PurePosixPath
from pydantic import BaseModel, Field
from .performance import PerformanceConfig, validate_plan

class AppConfig(BaseModel):
    app_id: str = ""
    device_id: str = ""
    reset: str = Field(default="reset",pattern="^(reset|retain)$")

# 根据类型校验 Python/JMX 及配置；辅助文件限制数量、大小、扩展名和文件名冲突。
# APP driver 由平台 fixture 管理，禁止用例自行重建一套驱动绕过设备租约和证据回收。
def validate_assets(kind,code,entry,files,config):
    if kind not in {"api","web","performance","app"}:raise ValueError("测试类型必须为 api/web/performance/app")
    if not code.strip() or len(code.encode())>150000:raise ValueError("用例内容不能为空，且不能超过 150KB")
    if kind=="performance":
        validate_plan(code);entry="plan.jmx";config=PerformanceConfig.model_validate(config).model_dump()
    else:
        tree=ast.parse(code,filename="test_case.py")
        if kind=="app" and any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=="driver" for n in ast.walk(tree)):
            raise ValueError("APP 用例使用平台 driver fixture，请删除自定义 driver；设备与应用通过 execution_config 配置")
        if entry!="test_case.py" and not entry.startswith("test_case.py::"):raise ValueError("入口必须为 test_case.py 或 test_case.py::测试函数")
        config=AppConfig.model_validate(config).model_dump() if kind=="app" else {}
    if len(files)>12:raise ValueError("每个版本最多 12 个辅助文件")
    for name,content in files.items():
        path=PurePosixPath(name)
        if path.is_absolute() or len(path.parts)!=1 or "\\" in name or name in {"test_case.py","plan.jmx","conftest.py","results.json"}:
            raise ValueError("辅助文件必须为不冲突的简单文件名")
        if path.suffix not in {".csv",".json",".txt",".py"} or len(content.encode())>150000:
            raise ValueError("辅助文件支持 csv/json/txt/py，单文件最多 150KB")
        if path.suffix==".py":ast.parse(content,filename=name)
    return entry,config

# 将执行内容生成稳定 SHA-256 摘要：源码/数据文件/配置改变，摘要也改变。
# 用途是版本核对，不是加密，不用于隐藏代码。
def version_hash(code,files,config):
    if not files and not config:return hashlib.sha256(code.encode()).hexdigest()
    return hashlib.sha256(json.dumps({"code":code,"files":files,"config":config},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
