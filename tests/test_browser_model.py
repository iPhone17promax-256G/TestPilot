import pytest
from aiqa.agent.browser_model import json_object


def test_gateway_can_wrap_complete_json_in_explanation():
    assert json_object('说明\\n{"action": [{"done": {"text": "ok"}}]}\\n结束')["action"][0]["done"]["text"] == "ok"


@pytest.mark.parametrize("content", ['{"action":', '{"a":1} then {"b":2}', '[{"a":1}]'])
def test_ambiguous_or_incomplete_gateway_output_is_rejected(content):
    with pytest.raises(ValueError):
        json_object(content)
