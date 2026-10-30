# browser-use 模型兼容层：部分 OpenAI 兼容网关会把结构化 JSON 包在说明文字/代码围栏里。
# 容错后仍需按框架要求的 schema 校验，不把任意文本直接当成浏览器动作。

"""Schema-validating compatibility for gateways that wrap JSON in prose/fences."""
import json
import re
from ..config import settings
from browser_use import ChatOpenAI


# 仅接受唯一完整 JSON 对象；若出现多个候选就报错，不随意挑一个执行。
# 功能：从网关返回的文字或 Markdown 围栏中识别唯一完整 JSON 对象，供浏览器动作结构校验。
# 输入：text 是供应商返回的原始文本；先尝试整个正文，再扫描其中可独立解码的 JSON 对象。
# 输出/影响：返回 Python 字典；不是对象或存在零个/多个候选时抛 ValueError；不执行其中动作、不保存文本。
def json_object(text):
    clean = re.sub(r"^\s*```(?:json)?\s*\n|\n```\s*$", "", text.strip())
    try:
        value = json.loads(clean)
        if not isinstance(value, dict):
            raise ValueError("模型必须返回 JSON 对象")
        return value
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        candidates = []
        cursor = 0
        while cursor < len(clean):
            start = clean.find("{", cursor)
            if start < 0:
                break
            try:
                value, size = decoder.raw_decode(clean[start:])
                candidates.append(value)
                cursor = start + size
            except json.JSONDecodeError:
                cursor = start + 1
        if len(candidates) != 1 or not isinstance(candidates[0], dict):
            raise ValueError("模型没有返回唯一、完整的 JSON 对象")
        return candidates[0]


# 继承 browser-use 的模型类，补供应商附加参数与输出校验；它不是主测试 Agent 的循环实现。
class BrowserModel(ChatOpenAI):
    # 功能：为 browser-use 的 OpenAI 兼容客户端包装请求入口，合并当前供应商要求的额外参数。
    # 输入：self 是已配置 base_url/模型/Key 的 BrowserModel；基础 client 来自父类 get_client，model_body 读取当前激活配置。
    # 输出/影响：返回带 chat.completions.create 的兼容包装对象；只创建内存封装，不在这里发模型请求或保存 Key。
    def get_client(self):
        from types import SimpleNamespace
        from ..config import model_body
        client=super().get_client()
        # 功能：在 browser-use 实际提交一次聊天请求时补充额外请求体，并转发给原供应商客户端。
        # 输入：kwargs 是框架传入的模型请求参数，可能含 messages、结构化格式及 extra_body；基础客户端和 self.base_url 从外层闭包获得。
        # 输出/影响：异步返回原 completions.create 的响应；会向模型供应商发请求并使用当前 Key，不将密钥写数据库或日志。
        async def create(**kwargs):
            kwargs["extra_body"]={**kwargs.get("extra_body",{}),**model_body(str(self.base_url))}
            return await client.chat.completions.create(**kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    # 功能：按 browser-use 约定异步调用模型，在要求结构化输出时增加 JSON 容错与原 schema 校验。
    # 输入：messages 是浏览器 Agent 的消息；output_format 是可选 Pydantic 输出类；kwargs 是父类支持的调用参数。
    # 输出/影响：返回父类的模型调用结果；无格式直接转发，有格式用临时子类校验返回文本；会请求模型但不在此操作浏览器。
    async def ainvoke(self, messages, output_format=None, **kwargs):
        if output_format is None:
            return await super().ainvoke(messages, **kwargs)
        class ValidatedFormat(output_format):
            # 功能：替换临时输出类的 JSON 文本解析，让夹带说明的结果仍按原动作 schema 严格验证。
            # 输入：cls 是临时 ValidatedFormat 类；data 是模型 JSON 文本；options 为 Pydantic 可选参数，此处未使用；原 output_format 来自外层调用。
            # 输出/影响：返回原 output_format 校验后的对象；错误抛校验/解析异常，不把任意文字直接当操作，也不保存或执行动作。
            @classmethod
            def model_validate_json(cls, data, **options):
                return output_format.model_validate(json_object(data))
        return await super().ainvoke(messages, output_format=ValidatedFormat, **kwargs)
