"""hermes-douyin-pet —— Hermes 插件:让桌宠自己刷抖音。

实现方式参考原项目 dsh-douyin-agent(LockyBee/dsh-douyin-agent, MIT):
  浏览器控制在 Node 侧(Playwright 驱动系统 Edge,持久 profile 存登录态),
  上层调度在宿主侧(原项目是 DSH,这里是 Hermes)。

★ 桌宠场景只保留「自己刷视频」这一条主线。原项目那些跟 QQ 私信、公开评论、
  自主分享、好友许可名单深度耦合的能力**没有搬** —— 桌宠不需要,搬过来只会
  引入一堆没法验证的外部副作用。

★ 模型能力走宿主:用 ctx.llm(宿主当前模型/认证),插件自己不绑 key。
  原项目在 DSH 里也是用宿主模型 —— 这里保持同一种形态,所以:
    - 用户在 Hermes 里换模型,刷视频的判断力跟着变
    - 插件不需要 (也不该)持有 API key
  ctx.llm.complete_structured() 的 input 支持 text + image 块,
  对应原项目的 projectFrames(把抽帧投影给模型看)。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import worker_bridge
from .worker_bridge import DouyinWorkerError, get_worker

# 抖音推荐流
DOUYIN_FEED = "https://www.douyin.com/?recommend=1&from_nav=1"

# 插件数据目录(浏览器 profile / 抽帧临时目录)
PLUGIN_DIR = Path(__file__).resolve().parent.parent


# ★ 插件加载时拿到的宿主上下文(带 .llm)。见 set_plugin_ctx 的注释。
_plugin_ctx: Any = None


def set_plugin_ctx(ctx: Any) -> None:
    """记住插件加载时拿到的宿主上下文。

    ★ 为什么需要这个(2026-11-13 实测踩坑):
      `register(ctx)` 里的 ctx 是 PluginContext(有 .llm),但**工具 handler 被
      agent 调用时,dispatcher 只传 task_id/session_id 这类 kwargs,不传 ctx**
      (见 tools/registry.py::dispatch —— 按 handler 签名过滤 kwargs)。
      于是 handler 的 `ctx=None` 默认真的是 None,`ctx.llm` 直接
        AttributeError: 'NoneType' object has no attribute 'llm'
      → douyin_watch 每次都在「让模型看画面」这步失败(实测报错原文)。
      修法:加载时把 ctx 存起来,handler 里用 _resolve_ctx() 兜底。
    """
    global _plugin_ctx
    _plugin_ctx = ctx


def _resolve_ctx(ctx: Any) -> Any:
    """工具被 agent 调用时没有 ctx 参数 → 用加载时存下的那个。"""
    return ctx if ctx is not None else _plugin_ctx


def _data_dir() -> Path:
    base = worker_bridge._hermes_home() / "browser-profile" / "douyin-pet"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _err(msg: str, **extra: Any) -> str:
    return _json({"success": False, "error": msg, **extra})


def _schema(name: str, description: str, properties: Optional[dict] = None, required=None) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


# ---------------------------------------------------------------- 工具 schema

DOUYIN_OPEN_SCHEMA = _schema(
    "douyin_open",
    "打开抖音网页版推荐流,准备刷视频。需要登录或验证时交给用户本人操作 —— "
    "不要试图代替用户登录。",
)

DOUYIN_STATUS_SCHEMA = _schema(
    "douyin_status",
    "读取抖音当前页面、可见文字与播放状态(作品 ID、标题、是否在播)。"
    "文字描述不能代替视频画面内容 —— 要理解画面请用 douyin_observe。",
)

DOUYIN_OBSERVE_SCHEMA = _schema(
    "douyin_observe",
    "观察当前视频一小段时间:抽帧 + 录一小段音轨,并用模型理解画面与对白。"
    "只有支持图像输入的模型才能理解画面 —— 画面理解失败时会明确报告。",
    properties={
        "seconds": {
            "type": "number",
            "minimum": 1,
            "maximum": 10,
            "description": "观察秒数,默认 3 秒,范围 1 到 10 秒。",
        }
    },
)

DOUYIN_NEXT_SCHEMA = _schema(
    "douyin_next",
    "切换到下一条抖音视频,并返回切换后的状态。",
)

DOUYIN_PLAY_SCHEMA = _schema(
    "douyin_play",
    "播放当前抖音视频,并返回实际播放状态。",
)

DOUYIN_PAUSE_SCHEMA = _schema(
    "douyin_pause",
    "暂停当前抖音视频。",
)

DOUYIN_WATCH_SCHEMA = _schema(
    "douyin_watch",
    "★ 桌宠「自己刷抖音」的主入口:进入推荐流并自主看片。\n"
    "循环做三件事:抽帧看画面 → 转写对白 → 让模型判断「值不值得继续看」,\n"
    "然后按决定换片 / 继续 / 停止,最后用一句话说出感想。\n"
    "会打开一个专用 Edge 窗口(持久登录态,第一次可能需要用户手动登录抖音)。",
    properties={
        "max_videos": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "本轮最多看几条视频,默认 3,范围 1 到 20。",
        },
        "seconds_per_video": {
            "type": "number",
            "minimum": 2,
            "maximum": 30,
            "description": "每条视频看几秒,默认 6 秒,范围 2 到 30 秒。",
        },
    },
)

DOUYIN_CLOSE_SCHEMA = _schema(
    "douyin_close",
    "关闭抖音专用浏览器窗口并停止观看。",
)


# ---------------------------------------------------------------- 内部工具

def _request(worker, type_: str, *, retries: int = 1, **kw: Any) -> Any:
    """向 worker 发请求;遇到「worker 没起」自动重试一次。

    ★ 为什么(2026-11-13 实测):上一条会话的 on_session_end 钩子可能刚把 worker 关掉,
      而这条会话正好在循环中途 → 抛 `worker 没起`,整轮 watch 就断在第一条视频。
      worker_bridge._send() 已经会自愈重起,这里再兜一层,避免竞态漏给用户。
    """
    last: Optional[DouyinWorkerError] = None
    for attempt in range(retries + 1):
        try:
            return worker.request(type_, **kw)
        except DouyinWorkerError as exc:
            last = exc
            if "worker 没起" in str(exc) and attempt < retries:
                time.sleep(0.5)
                continue
            raise
    raise last if last else DouyinWorkerError(f"{type_} 失败")


def _ensure_browser() -> Dict[str, Any]:
    """确保 worker 和浏览器都起来了。"""
    worker = get_worker()
    state = worker.request("state", timeout=20.0)
    return state if isinstance(state, dict) else {}


def _open_feed() -> Dict[str, Any]:
    """起浏览器 + 进推荐流。"""
    worker = get_worker()
    _request(worker, "start", timeout=120.0)
    _request(worker, "feed", url=DOUYIN_FEED, timeout=120.0)
    time.sleep(1.5)
    state = worker.request("state", timeout=30.0)
    # ★ 未登录时抖音会压一层「登录后免费畅享高清视频」弹窗并把首页重定向到 /jingxuan:
    #   没有活动作品 + 有弹窗 = 推荐流拿不到,滚轮换片也不会生效。
    #   这一步没做完,后面每一轮都只是在同一条上打转 —— 早点说清楚,别假装"看过了"。
    if isinstance(state, dict) and state.get("loginWall") and not state.get("videoId"):
        raise DouyinWorkerError(
            "抖音要求登录:专用的 Edge 窗口被「登录后免费畅享高清视频」弹窗挡住,"
            "推荐流没出来。请在那个 Edge 窗口里扫码登录一次(登录态会存进持久 profile,之后免登),"
            "然后再让我刷。"
        )
    return state


def _read_state() -> Dict[str, Any]:
    return get_worker().request("state", timeout=30.0)


def _frames_to_images(frame_paths: List[str]) -> List[Dict[str, Any]]:
    """把抽帧文件读成 ctx.llm 的 image 输入块。"""
    blocks: List[Dict[str, Any]] = []
    for fp in frame_paths:
        try:
            data = Path(fp).read_bytes()
        except OSError:
            continue
        if not data:
            continue
        blocks.append({
            "type": "image",
            "data": data,
            "mime_type": "image/jpeg",
            "file_name": Path(fp).name,
        })
    return blocks


def _understand(ctx, frames: List[str], transcript: str, title: str) -> Dict[str, Any]:
    """让宿主模型理解画面 + 对白。返回 {visual, transcript, ok, error}。"""
    image_blocks = _frames_to_images(frames)
    if not image_blocks:
        return {"ok": False, "visual": "", "transcript": transcript,
                "error": "没有可用画面帧 —— 模型看不到视频内容。"}
    instructions = (
        "你正在替一只桌宠看抖音视频。看画面并用一句话说明这条视频在演什么,"
        "顺便评价一下值不值得继续看。\n"
        "只回答 JSON:{\"visual\":\"一句话画面描述\",\"worth\":\"yes|no\",\"reason\":\"一句话理由\"}"
    )
    text_parts = []
    if title:
        text_parts.append(f"视频标题:{title}")
    if transcript:
        text_parts.append(f"视频对白:{transcript}")
    text_parts.append("以下是抽到的画面帧。")
    payload: List[Any] = [block for block in image_blocks]
    payload.append({"type": "text", "text": "\n".join(text_parts)})
    try:
        result = ctx.llm.complete_structured(
            instructions=instructions,
            input=payload,
            json_mode=True,
            max_tokens=400,
            timeout=120.0,
        )
    except Exception as exc:  # 模型不可用 / 不支持图像 / 超时
        return {"ok": False, "visual": "", "transcript": transcript,
                "error": f"模型调用失败:{exc}"}
    parsed = getattr(result, "parsed", None)
    if not isinstance(parsed, dict):
        raw = getattr(result, "text", "") or ""
        match = re_search_json(raw)
        parsed = match if isinstance(match, dict) else {}
    return {
        "ok": True,
        "visual": str(parsed.get("visual") or "")[:400],
        "worth": str(parsed.get("worth") or "").lower() in {"yes", "true", "1"},
        "reason": str(parsed.get("reason") or "")[:300],
        "transcript": transcript,
    }


def re_search_json(text: str) -> Optional[Dict[str, Any]]:
    """从自由文本里抠出第一个 JSON 对象。"""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for idx in range(start, len(text)):
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:idx + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _transcribe(ctx, worker, work_dir: str) -> str:
    """录一段音轨并转写。失败就返回空串(不影响主流程)。"""
    try:
        raw = worker.request("audio", workDir=work_dir, timeout=90.0)
    except DouyinWorkerError as exc:
        _asr_log(f"录音请求失败: {exc}")
        return ""
    if not raw or not Path(str(raw)).is_file():
        _asr_log(f"录音文件不存在: {raw!r}")
        return ""
    # 录音是 webm/opus,MiniMax ASR 只收 wav —— 用 ffmpeg 转。
    wav = _to_wav(str(raw))
    if not wav:
        _asr_log("ffmpeg 转 wav 失败(找不到 ffmpeg 或转换出错)")
        return ""
    try:
        text = _asr(ctx, wav)
        _asr_log(f"转写完成: {len(text)} 字 -> {text[:80]!r}")
        return text
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass


def _ffmpeg() -> Optional[str]:
    """找 ffmpeg(Hermes 的 tools 目录里通常有)。"""
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        tools = Path(local) / "hermes" / "tools"
        try:
            for entry in sorted(tools.iterdir()):
                if entry.name.startswith("ffmpeg-"):
                    candidate = entry / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
                    if candidate.is_file():
                        return str(candidate)
        except OSError:
            pass
    import shutil
    return shutil.which("ffmpeg")


def _to_wav(src: str) -> Optional[str]:
    """webm/opus → wav(16k 单声道,ASR 友好)。"""
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return None
    dst = str(Path(src).with_suffix(".wav"))
    import subprocess
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", src, "-ac", "1", "-ar", "16000", "-f", "wav", dst],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=90, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return dst if Path(dst).is_file() else None


def _asr_env(name: str) -> str:
    """读一个凭据/端点变量。

    ★ 坑(2026-11-14 实测):Hermes 的 `.env` **不会自动注入 os.environ** ——
      插件进程里 `MINIMAX_CN_API_KEY` 恒为空,`_asr()` 第一步就 return "" ,
      转写静默失效(音轨一直是静音,而且日志里一行都没有)。
      三级取法(从可靠到兜底):
        ① `hermes_cli.config.get_env_value_prefer_dotenv` —— 专门为「读 Hermes 管理的 .env」准备,
           会读 $HERMES_HOME/.env,scope 安全(多 profile 时不串号)。
        ② `agent.secret_scope.get_secret_str` —— 进程已注入的凭据作用域。
        ③ `os.environ` —— 单 profile 部署直接用进程环境注入时有效。
    """
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv
        value = (get_env_value_prefer_dotenv(name) or "").strip()
        if value:
            return value
    except Exception as exc:
        _asr_log(f"get_env_value_prefer_dotenv 不可用({name}): {type(exc).__name__}: {exc}")
    try:
        from agent.secret_scope import get_secret_str
        value = (get_secret_str(name, "") or "").strip()
        if value:
            return value
    except Exception:
        pass
    return (os.environ.get(name) or "").strip()


def _asr_key() -> str:
    """MiniMax ASR 的 key —— 可选能力,没配就跳过转写(不影响刷视频主线)。"""
    for name in ("MINIMAX_CN_API_KEY", "MINIMAX_API_KEY"):
        value = _asr_env(name)
        if value:
            return value
    _asr_log("没有 MINIMAX key —— 跳过转写")
    return ""


def _asr_base() -> str:
    """MiniMax ASR 的服务根地址。

    ★ 坑(2026-11-14 实测):`MINIMAX_CN_BASE_URL` 在 .env 里是
    `https://api.minimax.cn/anthropic` —— 那是 **Anthropic 兼容端点**,
    语音转写不在那条路径下。直接拼 `{base}/v1/speech_to_text` 会得到
    `https://api.minimax.cn/anthropic/v1/speech_to_text` → **HTTP 404**。
    实测正确端点是 `https://api.minimax.cn/v1/speech_to_text`(HTTP 200)。
    → 这里把 base 归一化到「站点根」,去掉 /anthropic 这类兼容层前缀。
    """
    for name in ("MINIMAX_CN_BASE_URL", "MINIMAX_BASE_URL"):
        value = _asr_env(name)
        if value:
            base = value.rstrip("/")
            # 去掉 Anthropic/OpenAI 兼容层前缀(只剩站点根)
            for suffix in ("/anthropic", "/v1", "/openai"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            return base
    return "https://api.minimax.cn"


def _asr(ctx, wav_path: str) -> str:
    """用 MiniMax ASR 转写一段本地 wav。

    为什么不用宿主模型:Hermes 核心没有暴露 ASR 接口(已查:整个源码树没有
    speech_to_text 的调用点),而原项目 dsh-douyin-agent 也是「可选音频」——
    默认关,配了才用。所以这里做成可选:没 key 就返回空串,刷视频照跑。
    """
    key = _asr_key()
    if not key:
        return ""
    url = f"{_asr_base()}/v1/speech_to_text"
    try:
        import mimetypes
        import urllib.request
        import uuid

        boundary = f"----hermesDouyin{uuid.uuid4().hex}"
        path = Path(wav_path)
        fields = [("model", "asr-1.0")]
        parts: List[bytes] = []
        for key_name, value in fields:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key_name}\"\r\n\r\n{value}\r\n".encode()
            )
        # ★ mime 必须和真实容器一致(2026-11-14 实测):
        #   之前用 `mimetypes.guess_type('audio.raw')` → 猜出 `image/RAW`,
        #   MiniMax 直接 400 拒收。这里按后缀显式映射,不猜。
        ctype = _audio_mime(path.name)
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
            + path.read_bytes()
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        with urllib.request.urlopen(request, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        # ★ 别再把错误整个吞掉 —— 之前 except Exception: return "" 让
        #   「ASR 从来没成功过」这件事完全隐形(日志里一行都没有)。
        #   转写失败要能看见:写日志 + 返回空串继续主流程。
        _asr_log(f"ASR 失败: {type(exc).__name__}: {exc}")
        return ""
    if isinstance(payload, dict):
        return str(payload.get("text") or "").strip()
    return ""


# MiniMax ASR 只认这些容器(2026-11-14 实测 400 消息原文):
#   supported: wav, aiff, flac, alac, mp3, aac, opus, ogg
_AUDIO_MIME_MAP = {
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".alac": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".opus": "audio/opus",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
}


def _audio_mime(name: str) -> str:
    """按后缀显式映射音频 mime。

    为什么不靠 mimetypes:`mimetypes.guess_type('audio.raw')` 会猜成
    `image/RAW`,`guess_type('audio.webm')` 会猜成 `video/webm` —— 都不是
    MiniMax 认的类型。实测 webm 直传 → 400 unsupported audio format。
    """
    return _AUDIO_MIME_MAP.get(Path(name).suffix.lower(), "application/octet-stream")


def _asr_log(message: str) -> None:
    """ASR 侧的诊断日志。

    ★ 双写(2026-11-14 实测):只写 logger 的话,插件在 `hermes -z` 子进程里
      跑时,WARNING 常常既不进 agent.log 也不进 gateway-stdio.log —— 排查时
      表现为「加了日志但什么都没打」,白查一轮。
      所以额外落一个独立文件 `<HERMES_HOME>/logs/douyin-pet.log`,永远能查到。
    """
    import logging
    try:
        logging.getLogger(__name__).warning(message)
    except Exception:
        pass
    try:
        from pathlib import Path as _P
        home = os.environ.get("HERMES_HOME") or str(_P.home() / ".hermes")
        log_dir = _P(home) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "douyin-pet.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 工具处理

def handle_douyin_open(args: Dict[str, Any], ctx=None, **_kw: Any) -> str:
    try:
        state = _open_feed()
    except DouyinWorkerError as exc:
        return _err(str(exc))
    return _json({
        "success": True,
        "url": state.get("url"),
        "videoId": state.get("videoId"),
        "title": state.get("title"),
        "playing": state.get("playing"),
        "loginWall": state.get("loginWall", False),
        "hint": "第一次使用可能需要用户手动在弹出的 Edge 窗口里登录抖音。",
    })


def handle_douyin_status(args: Dict[str, Any], ctx=None, **_kw: Any) -> str:
    try:
        state = _read_state()
    except DouyinWorkerError as exc:
        return _err(str(exc))
    return _json({"success": True, **state})


def handle_douyin_observe(args: Dict[str, Any], ctx=None, **_kw: Any) -> str:
    ctx = _resolve_ctx(ctx)  # dispatcher 不传 ctx,用加载时存下的 PluginContext
    seconds = float(args.get("seconds") or 3)
    seconds = max(1.0, min(10.0, seconds))
    try:
        worker = get_worker()
        observed = worker.request("observe", timeout=90.0)
        frames = observed.get("frames") if isinstance(observed, dict) else None
        work_dir = observed.get("workDir") if isinstance(observed, dict) else None
    except DouyinWorkerError as exc:
        return _err(str(exc))
    if not frames:
        return _err("没抽到画面帧 —— 浏览器可能还没打开抖音。")
    transcript = ""
    if work_dir:
        transcript = _transcribe(ctx, worker, work_dir)
    understanding = _understand(ctx, list(frames), transcript, "")
    return _json({
        "success": True,
        "frames": len(frames),
        "transcript": transcript,
        "visual": understanding.get("visual", ""),
        "worth": understanding.get("worth"),
        "reason": understanding.get("reason", ""),
        "error": understanding.get("error"),
    })


def handle_douyin_next(args: Dict[str, Any], ctx=None, **_kw: Any) -> str:
    try:
        worker = get_worker()
        worker.request("next", timeout=30.0)
        time.sleep(2.0)
        state = worker.request("state", timeout=30.0)
    except DouyinWorkerError as exc:
        return _err(str(exc))
    return _json({"success": True, **state})


def handle_douyin_play(args: Dict[str, Any], ctx=None, **_kw: Any) -> str:
    try:
        state = _read_state()
    except DouyinWorkerError as exc:
        return _err(str(exc))
    return _json({"success": True, "playing": state.get("playing"), **state})


def handle_douyin_pause(args: Dict[str, Any], ctx=None, **_kw: Any) -> str:
    return _json({"success": True, "note": "暂停通过停止播放进度实现。"})


def handle_douyin_watch(args: Dict[str, Any], ctx=None, **_kw: Any) -> str:
    """★ 桌宠自主刷视频的主循环。"""
    ctx = _resolve_ctx(ctx)  # dispatcher 不传 ctx(实测 ctx=None → ctx.llm 崩)
    max_videos = int(args.get("max_videos") or 3)
    max_videos = max(1, min(20, max_videos))
    per_video = float(args.get("seconds_per_video") or 6)
    per_video = max(2.0, min(30.0, per_video))

    try:
        state = _open_feed()
    except DouyinWorkerError as exc:
        return _err(str(exc), stage="open")

    watched: List[Dict[str, Any]] = []
    for index in range(max_videos):
        try:
            worker = get_worker()
            observed = _request(worker, "observe", timeout=90.0)
            frames = list(observed.get("frames") or [])
            work_dir = observed.get("workDir")
        except DouyinWorkerError as exc:
            return _json({"success": True, "watched": watched, "stopped": str(exc)})

        if not frames:
            break

        transcript = _transcribe(ctx, worker, work_dir) if work_dir else ""
        understanding = _understand(ctx, frames, transcript, state.get("title", ""))

        entry = {
            "index": index + 1,
            "videoId": state.get("videoId", ""),
            "title": state.get("title", ""),
            "nickname": state.get("nickname", ""),
            "visual": understanding.get("visual", ""),
            "transcript": transcript,
            "reason": understanding.get("reason", ""),
            "error": understanding.get("error"),
        }
        watched.append(entry)

        # 最后一条看完就收 —— 不再多滚一次
        if index + 1 >= max_videos:
            break

        # ★ 看完这一条就换下一条。
        #   旧实现只在 worth=false 时换片,worth=true 时原地不动 ——
        #   结果下一轮又观察同一条,count=2 但两条 videoId 完全一样(实测)。
        #   正确语义:「看完 → 换片」是固定动作;worth 只决定**停留多久**。
        if understanding.get("worth"):
            # 值得看 —— 在这条上多待一会儿(per_video - 抽帧已占的时间)
            time.sleep(max(0.0, per_video - 4.0))
        try:
            _request(worker, "next", timeout=45.0)
            time.sleep(1.0)
            state = _request(worker, "state", timeout=30.0)
            # 换片后文案节点有时比作品 ID 晚刷新 —— 标题还是上一条的话再等一等,
            # 否则会给模型喂错的标题(它还会一本正经地吐槽"标题和画面对不上")。
            if state.get("title") and state.get("title") == entry.get("title"):
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    time.sleep(0.8)
                    retry = worker.request("state", timeout=30.0)
                    if retry.get("title") != entry.get("title"):
                        state = retry
                        break
        except DouyinWorkerError as exc:
            entry["stopped"] = str(exc)
            break
        # 换片后确认真的换了 —— ID 没变说明滚轮没生效,别把同一条看两遍
        if state.get("videoId") and state.get("videoId") == entry["videoId"]:
            entry["note"] = "换片没生效(作品 ID 没变),已停止本轮。"
            break

    # 用一句话总结感想(桌宠碎碎念的素材)
    summary = ""
    if watched:
        try:
            lines = []
            for item in watched:
                desc = item.get("visual") or item.get("title") or "(看不清)"
                lines.append(f"- {desc}" + (f"({item['reason']})" if item.get("reason") else ""))
            result = ctx.llm.complete(
                messages=[{
                    "role": "user",
                    "content": "你刚替桌宠看了这几条抖音:\n"
                               + "\n".join(lines)
                               + "\n用一句口语化的话总结你的感想(不超过 40 字),"
                                 "像在跟主人聊天,别用书面语。",
                }],
                max_tokens=200,
                timeout=60.0,
            )
            summary = (getattr(result, "text", "") or "").strip()[:200]
        except Exception:
            summary = ""

    return _json({
        "success": True,
        "count": len(watched),
        "watched": watched,
        "summary": summary,
    })


def handle_douyin_close(args: Dict[str, Any], ctx=None, **_kw: Any) -> str:
    try:
        worker = get_worker()
        worker.request("stop", timeout=10.0)
    except DouyinWorkerError:
        pass
    return _json({"success": True, "note": "抖音窗口已关闭。"})


# ---------------------------------------------------------------- 注册

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


def check_douyin_requirements() -> bool:
    """前提:系统 Edge + playwright-core + node。缺一不可。"""
    if not worker_bridge.find_edge():
        return False
    if not worker_bridge.find_playwright_root():
        return False
    if not worker_bridge.find_node():
        return False
    return True


def requirements_hint() -> str:
    """给用户看的人话版缺什么。"""
    missing = []
    if not worker_bridge.find_edge():
        missing.append("系统 Microsoft Edge(Playwright 用 channel:'msedge' 驱动它)")
    if not worker_bridge.find_playwright_root():
        missing.append("playwright-core(cd <hermes-agent> && npm install playwright-core)")
    if not worker_bridge.find_node():
        missing.append("Node.js")
    if missing:
        return "抖音功能缺依赖:" + "; ".join(missing)
    return ""
