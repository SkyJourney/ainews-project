"""llm_client.py 配置断言测试：验证 Instructor 客户端固定走 Mode.JSON、调用时不涉及
tool_choice——这是项目踩过两次坑才定论的边界约束（M0 实测强制 tool_choice 因模型而异，
M1 进一步实测 Instructor 的 Mode.TOOLS 会忽略显式 tool_choice，见
.claude/memory/decisions.md），但此前没有任何测试断言这一行配置本身
（见 .claude/memory/known_issues.md）。
"""

from __future__ import annotations

import instructor

from worker import llm_client


def test_get_client_uses_mode_json_not_tool_choice(mocker):
    llm_client._get_client.cache_clear()
    mocker.patch.dict("os.environ", {"LITELLM_BASE_URL": "http://fake-gateway", "LITELLM_API_KEY": "fake-key"})
    fake_instructor_client = mocker.MagicMock()
    from_openai = mocker.patch.object(instructor, "from_openai", return_value=fake_instructor_client)

    result = llm_client._get_client()

    from_openai.assert_called_once()
    _, kwargs = from_openai.call_args
    assert kwargs.get("mode") == instructor.Mode.JSON
    assert "tool_choice" not in kwargs
    assert result is fake_instructor_client

    llm_client._get_client.cache_clear()  # 不污染其余测试（@lru_cache(maxsize=1)）


def test_call_structured_never_passes_tool_choice(mocker):
    fake_client = mocker.MagicMock()
    mocker.patch.object(llm_client, "_get_client", return_value=fake_client)

    class _FakeModel:
        pass

    llm_client.call_structured(
        model="glm-5-turbo",
        system_prompt="system",
        user_content="user",
        response_model=_FakeModel,
    )

    _, kwargs = fake_client.chat.completions.create.call_args
    assert "tool_choice" not in kwargs
    assert kwargs["model"] == "glm-5-turbo"
    assert kwargs["response_model"] is _FakeModel
    assert kwargs["max_retries"] == llm_client.DEFAULT_MAX_RETRIES
    assert kwargs["max_tokens"] == llm_client.DEFAULT_MAX_TOKENS
