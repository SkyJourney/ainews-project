"""封装 instructor + openai SDK，统一经自建 LiteLLM 网关发起结构化输出调用。

决策依据见 .claude/memory/decisions.md：
- 「经 Instructor 时统一用 Mode.JSON，不是 tool_choice=auto（M1 修正）」：Instructor 的
  Mode.TOOLS 会忽略/覆盖显式传入的 tool_choice，自己强制指向该 function，导致
  deepseek-v4-flash 这类模型报错；改用 Mode.JSON（response_format=json_object + prompt
  注入 schema）彻底绕开 tool_choice。
- 「max_retries 从 0 改为 1（M1 二次修正）」：deepseek-v4-flash 是 DeepSeek 官方文档也
  承认的已知问题（reasoning 模型偶尔把答案整个写进 reasoning_content，content 字段留空，
  finish_reason 仍是 'stop'），不是随机抽风也不是 token 预算问题——实测 max_retries=1
  （让 Instructor 把校验错误反馈给模型重新生成一次）能稳定修复，比自建全新客户端的改动小
  得多，继续用 Mode.JSON 也不用为了这个问题换模型。
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TypeVar

import instructor
from openai import OpenAI
from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)

# 不依赖网关对各模型的默认 max_tokens（Mode.JSON 注入 schema 说明后 prompt_tokens
# 明显变长，默认值可能偏紧），显式给一个宽松上限。
DEFAULT_MAX_TOKENS = 8000

# 见模块顶部说明：deepseek-v4-flash 偶发把答案写进 reasoning_content 而非 content，
# Instructor 的重试机制（把校验错误反馈给模型）能有效自纠正，固定用 1。
DEFAULT_MAX_RETRIES = 1


@lru_cache(maxsize=1)
def _get_client() -> instructor.Instructor:
    openai_client = OpenAI(
        base_url=os.environ["LITELLM_BASE_URL"],
        api_key=os.environ["LITELLM_API_KEY"],
    )
    return instructor.from_openai(openai_client, mode=instructor.Mode.JSON)


def call_structured(
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    response_model: type[ModelT],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> ModelT:
    """发起一次结构化输出调用（Mode.JSON，不涉及 tool_choice）。"""
    client = _get_client()
    return client.chat.completions.create(
        model=model,
        max_retries=max_retries,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_model=response_model,
    )
