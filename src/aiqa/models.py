# 按账号保存模型元信息；平台记录里没有模型 API Key 字段。
# 思考深度必须来自已确认的模型能力，OpenAI 兼容接口并没有通用的能力发现标准。

"""Public model metadata. Credentials never enter platform records."""
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
import httpx
from pydantic import BaseModel, ConfigDict, Field
from .storage import Record, session, uid
from .workspace import current_user

EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
_catalog = (0, [])

class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = ""
    name: str = Field(min_length=1, max_length=120)
    provider: str = "openrouter"
    baseUrl: str = "https://openrouter.ai/api/v1"
    model: str = Field(min_length=1, max_length=160)
    maxSteps: int = Field(default=24, ge=5, le=50)
    maxTokens: int = Field(default=12000, ge=1000, le=32000)
    default: bool = False
    freeOnly: bool = True
    vision: bool = False
    tools: bool = False
    reasoningOptions: list[str] = Field(default_factory=list, max_length=7)
    reasoningSource: str = "unknown"
    extraBody: dict = Field(default_factory=dict)

    def public(self):
        parsed = urlparse(self.baseUrl)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("模型地址必须是没有凭据和查询参数的 HTTP API 地址")
        if self.provider not in {"openrouter", "compatible"}: raise ValueError("供应商类型不正确")
        if self.provider == "openrouter" and self.baseUrl.rstrip("/") != "https://openrouter.ai/api/v1":
            raise ValueError("OpenRouter 地址固定为官方 API")
        if any(v not in EFFORTS for v in self.reasoningOptions): raise ValueError("思考深度不合法")
        # Only harmless model switches are accepted; credentials and arbitrary headers are forbidden.
        if set(self.extraBody) - {"thinking"}: raise ValueError("附加参数仅允许 thinking，不能保存密钥或请求头")
        if self.extraBody and self.extraBody not in ({"thinking": {"type": "disabled"}}, {"thinking": {"type": "enabled"}}):
            raise ValueError("thinking 参数格式不正确")
        return self.model_dump()


# 把 OpenRouter 目录字段转成平台所需能力：图片、工具、价格、思考档位。
# 元信息只代表目录声明；实际是否能调用还需本地 model_test 和真实任务验证。
def model_metadata(item):
    reasoning = item.get("reasoning") or {}
    efforts = reasoning.get("supported_efforts", [])
    if efforts is None: efforts = EFFORTS
    efforts = [e for e in EFFORTS if e in efforts and not (e == "none" and reasoning.get("mandatory"))]
    pricing = item.get("pricing", {})
    try: free = Decimal(pricing.get("prompt", "-1")) == 0 and Decimal(pricing.get("completion", "-1")) == 0
    except InvalidOperation: free = False
    return dict(id=item["id"], name=item.get("name", item["id"]), vision="image" in item.get("architecture", {}).get("input_modalities", []),
                tools="tools" in item.get("supported_parameters", []), free=free, reasoningOptions=efforts,
                reasoningSource="OpenRouter 模型目录" if "supported_efforts" in reasoning else "unknown")


def catalog(refresh=False):
    global _catalog
    if not refresh and time.monotonic() - _catalog[0] < 300: return _catalog[1]
    response = httpx.get("https://openrouter.ai/api/v1/models", timeout=20, follow_redirects=False)
    response.raise_for_status()
    rows = [model_metadata(item) for item in response.json()["data"]]
    _catalog = (time.monotonic(), rows)
    return rows


# 检查账号归属后保存元信息；免费预设要求具体模型同时支持视觉和工具且输入/输出价格为零。
# 兼容供应商未知思考档位时清空选项，避免猜测 low/high 导致请求报错。
def save_profile(body):
    from .executors import profiles
    payload = body.public()
    if body.provider == "openrouter":
        item = next((m for m in catalog() if m["id"] == body.model), None)
        if not item: raise ValueError("OpenRouter 模型目录中没有这个模型")
        if body.freeOnly and (not item["free"] or not item["vision"] or not item["tools"] or body.model == "openrouter/free"):
            raise ValueError("免费预设必须选择同时支持视觉和工具调用的具体免费模型")
        payload.update({k: item[k] for k in ["vision", "tools", "reasoningOptions", "reasoningSource"]})
    else:
        # OpenAI-compatible model IDs do not provide a universal effort discovery API.
        payload.update(reasoningOptions=[], reasoningSource="unknown")
    identity = body.id or uid("MODEL")
    with session() as db:
        row = db.get(Record, identity)
        if row and (row.kind != "model_profile" or row.payload.get("ownerId") != current_user()):
            raise PermissionError("模型配置不属于当前账号")
        payload.update(id=identity, ownerId=current_user())
        if not profiles(): payload["default"] = True
        if payload["default"]:
            from sqlalchemy import select
            for other in db.scalars(select(Record).where(Record.kind == "model_profile")):
                if other.id != identity and other.payload.get("ownerId") == current_user(): other.payload = dict(other.payload, default=False)
        if row: row.payload = payload
        else: db.add(Record(id=identity, kind="model_profile", payload=payload))
    return payload


def delete_profile(identity):
    with session() as db:
        row = db.get(Record, identity)
        if not row or row.kind != "model_profile" or row.payload.get("ownerId") != current_user(): raise PermissionError("模型配置不属于当前账号")
        db.delete(row)


def validate_selection(identity, effort=""):
    from .executors import profile, profiles
    if not identity:
        rows = profiles()
        identity = next((r["id"] for r in rows if r.get("default")), rows[0]["id"] if rows else "")
    if not identity: raise ValueError("请先连接本地执行器并添加模型")
    p = profile(identity)
    if effort and effort not in p.get("reasoningOptions", []): raise ValueError("该模型未确认支持这个思考深度")
    return p


# 根据供应商生成实际请求附加参数；OpenRouter 免费模式限制零价格，不默默回退付费。
def request_body(profile, effort=""):
    extra = dict(profile.get("extraBody", {}))
    if profile["provider"] == "openrouter":
        if effort:
            if effort not in profile.get("reasoningOptions", []): raise ValueError("该模型不支持这个思考深度")
            extra["reasoning"] = {"effort": effort}
        extra["provider"] = {"require_parameters": True}
        if profile.get("freeOnly"): extra["provider"]["max_price"] = {"prompt": 0, "completion": 0}
    return extra

active_profile = {}
active_selection = None


# 只在本地任务子进程从 LocalVault 取 Key，绑定本轮模型与思考深度。
# 这些全局变量属于当前进程；不同账号/并行任务的子进程不会因此共用同一个模型 Key。
def activate_local(profile, effort, account):
    global active_profile, active_selection
    from .config import settings
    from .local_vault import model_vault
    key=model_vault(account).for_profile(profile)
    if not key:raise ValueError("本地没有该模型的 API Key，请在模型设置中填写")
    active_profile=dict(profile)
    active_selection=(profile["id"],effort)
    settings.model_key=key
    settings.model_url=profile["baseUrl"]
    settings.model=profile["model"]
    settings.model_max_tokens=profile["maxTokens"]
    settings.model_extra_body=request_body(profile,effort)
    settings.model_reasoning_effort=""
