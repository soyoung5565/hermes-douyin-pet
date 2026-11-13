"""hermes-douyin-pet —— 让 Hermes 桌宠自己刷抖音。

参考原项目 dsh-douyin-agent(LockyBee/dsh-douyin-agent, MIT)的实现方式:
  浏览器控制在 Node 侧(Playwright 驱动系统 Edge + 持久 profile 存登录态),
  上层调度在宿主侧 —— 原项目宿主是 DSH,这里宿主是 Hermes。

模型能力走宿主(ctx.llm):插件自己不绑 API key,用户在 Hermes 里换模型,
刷视频的判断力跟着变。这与原项目在 DSH 里用宿主模型是同一种形态。

暴露给 agent 的工具:
  douyin_open    打开推荐流
  douyin_status  读当前页面/播放状态
  douyin_observe 抽帧 + 录音 + 让模型理解画面
  douyin_next    换下一条
  douyin_play    继续播放
  douyin_pause   暂停
  douyin_watch   ★ 自主刷视频主入口(抽帧→理解→判断→换片→说感想)
  douyin_close   关闭浏览器
"""

from __future__ import annotations

import logging

from .tools import (
    DOUYIN_CLOSE_SCHEMA,
    DOUYIN_NEXT_SCHEMA,
    DOUYIN_OBSERVE_SCHEMA,
    DOUYIN_OPEN_SCHEMA,
    DOUYIN_PAUSE_SCHEMA,
    DOUYIN_PLAY_SCHEMA,
    DOUYIN_STATUS_SCHEMA,
    DOUYIN_WATCH_SCHEMA,
    check_douyin_requirements,
    handle_douyin_close,
    handle_douyin_next,
    handle_douyin_observe,
    handle_douyin_open,
    handle_douyin_pause,
    handle_douyin_play,
    handle_douyin_status,
    handle_douyin_watch,
    requirements_hint,
)

logger = logging.getLogger(__name__)

_TOOLS = (
    ("douyin_open", DOUYIN_OPEN_SCHEMA, handle_douyin_open, "🎵"),
    ("douyin_status", DOUYIN_STATUS_SCHEMA, handle_douyin_status, "📍"),
    ("douyin_observe", DOUYIN_OBSERVE_SCHEMA, handle_douyin_observe, "👀"),
    ("douyin_next", DOUYIN_NEXT_SCHEMA, handle_douyin_next, "⏭️"),
    ("douyin_play", DOUYIN_PLAY_SCHEMA, handle_douyin_play, "▶️"),
    ("douyin_pause", DOUYIN_PAUSE_SCHEMA, handle_douyin_pause, "⏸️"),
    ("douyin_watch", DOUYIN_WATCH_SCHEMA, handle_douyin_watch, "📺"),
    ("douyin_close", DOUYIN_CLOSE_SCHEMA, handle_douyin_close, "🔒"),
)


def _on_session_end(**_kwargs) -> None:
    """会话结束时别留下孤儿 Edge 进程。"""
    try:
        from . import worker_bridge
        worker = worker_bridge._worker
        if worker is not None:
            worker.stop()
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("hermes-douyin-pet on_session_end cleanup failed: %s", exc)


def register(ctx) -> None:
    """注册工具(插件加载器调用一次)。"""
    # ★ 把 PluginContext 存给工具 handler 用。
    #   工具 handler 被 agent 调用时 dispatcher 不传 ctx(实测 ctx=None),
    #   不存下来 `ctx.llm` 就是 None.llm —— 见 tools.set_plugin_ctx 的注释。
    from .tools import set_plugin_ctx
    set_plugin_ctx(ctx)

    hint = requirements_hint()
    if hint:
        # 不静默失败:依赖缺失时把原因说出来,但仍然注册工具 ——
        # 这样 agent 调它时能拿到明确错误,而不是「工具不存在」这种没头没脑的失败。
        logger.warning("hermes-douyin-pet: %s", hint)

    def _bind(handler):
        """把宿主 ctx 绑进 handler。

        ★ 必须绑:registry.dispatch() 只把「handler 签名里声明过的上下文 kwargs」
        (task_id / session_id / ...)传进去,`ctx` 不在其中 —— 直接注册裸 handler
        的话 ctx 永远是 None,抽帧后的模型理解会以
        `'NoneType' object has no attribute 'llm'` 失败(实测)。
        """

        def _call(args, **kwargs):
            return handler(args, ctx=ctx, **kwargs)

        return _call

    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="hermes_douyin_pet",
            schema=schema,
            handler=_bind(handler),
            check_fn=check_douyin_requirements,
            emoji=emoji,
        )

    ctx.register_hook("on_session_end", _on_session_end)


__all__ = ["register"]
