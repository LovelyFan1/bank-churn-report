"""LLM 接入 —— 全系统唯一的模型出口。

**为什么单独一个文件**

换模型（DeepSeek → 通义 → 智谱）只应改这一处。若各节点各建一个 client，
就会出现「路由器用 deepseek-chat、写手用 qwen-max」这种隐性不一致，
且 key/超时/重试配置会散落各处。

**两个实例，职责不同（关键设计）**

    llm_router   temperature=0  只输出结构化意图，不写面向用户的文字
    llm_writer   temperature=0  只把已算好的数字组织成中文

把「理解」与「表达」分开，是为了让幻觉无处藏身：
路由器见不到数字（它只选工具），写手见不到工具定义（它只能重组给定内容）。

**为什么 temperature 都是 0**

本系统是数据分析工具，不是创意助手。同一个问题应该给同样的答案 ——
否则用户两次问同一个客户得到不同结论，系统的可信度直接归零。
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

_router = None
_writer = None


class AgentDisabled(RuntimeError):
    """Agent 未启用（缺 key 或显式关闭）。

    ⚠ 宁可明确报错，也不静默降级成规则匹配 —— 后者会让调用方以为
      「大模型在回答」，实际没有，属于最坏的一类不一致。
    """


def _build(temperature: float = 0.0):
    from langchain_openai import ChatOpenAI

    if not settings.AGENT_ENABLED:
        raise AgentDisabled(
            "智能体未启用。请设置环境变量 AGENT_ENABLED=true 并提供 "
            "AGENT_LLM_API_KEY（见 backend/app/config.py 的智能体配置节）。"
        )
    if not settings.AGENT_LLM_API_KEY:
        raise AgentDisabled(
            "缺少 AGENT_LLM_API_KEY。请通过 .env 注入（.env 已在 .gitignore 中），"
            "不要把 key 写进源码。"
        )
    return ChatOpenAI(
        model=settings.AGENT_LLM_MODEL,
        api_key=settings.AGENT_LLM_API_KEY,
        base_url=settings.AGENT_LLM_BASE_URL,
        temperature=temperature,
        timeout=settings.AGENT_TIMEOUT,
        # 失败重试交给框架：网络抖动是常态（本项目实测过 Docker 转发链路重置）
        max_retries=2,
    )


def router_llm():
    """意图识别与工具选择用的实例。"""
    global _router
    if _router is None:
        _router = _build(temperature=0.0)
        logger.info("agent: router LLM 已初始化 (%s @ %s)",
                    settings.AGENT_LLM_MODEL, settings.AGENT_LLM_BASE_URL)
    return _router


def writer_llm():
    """组织回答用的实例。"""
    global _writer
    if _writer is None:
        _writer = _build(temperature=0.0)
    return _writer


def reset() -> None:
    """清空缓存的实例 —— 供测试在改配置后重建。"""
    global _router, _writer
    _router = None
    _writer = None


def is_available() -> tuple[bool, str]:
    """检查 Agent 是否可用，返回 (可用, 原因)。供接口层给出明确提示。"""
    if not settings.AGENT_ENABLED:
        return False, "AGENT_ENABLED=false"
    if not settings.AGENT_LLM_API_KEY:
        return False, "缺少 AGENT_LLM_API_KEY"
    try:
        import langchain_openai  # noqa: F401
        import langgraph  # noqa: F401
    except ImportError as e:
        # 依赖未装（例如镜像未重建）—— 这是部署问题，必须明确报出
        return False, f"依赖未安装: {e.name}"
    return True, "ok"
