# browser-use 模型兼容层：部分 OpenAI 兼容网关会把结构化 JSON 包在说明文字/代码围栏里。
# 容错后仍需按框架要求的 schema 校验，不把任意文本直接当成浏览器动作。

"""Schema-validating compatibility for gateways that wrap JSON in prose/fences."""
import json
import re
from ..config import settings
from browser_use import ChatOpenAI


# 仅接受唯一完整 JSON 对象；若出现多个候选就报错，不随意挑一个执行。
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
    def get_client(self):
        from types import SimpleNamespace
        from ..config import model_body
        client=super().get_client()
        async def create(**kwargs):
            kwargs["extra_body"]={**kwargs.get("extra_body",{}),**model_body(str(self.base_url))}
            return await client.chat.completions.create(**kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def ainvoke(self, messages, output_format=None, **kwargs):
        if output_format is None:
            return await super().ainvoke(messages, **kwargs)
        class ValidatedFormat(output_format):
            @classmethod
            def model_validate_json(cls, data, **options):
                return output_format.model_validate(json_object(data))
        return await super().ainvoke(messages, output_format=ValidatedFormat, **kwargs)
