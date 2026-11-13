"""★ 真实 Hermes 会话里调插件工具 —— 端到端验收。

这个脚本做的是「桌宠真正会做的事」:
  1. 用桌宠那条提示词构造 prompt
  2. 跑 hermes -z <prompt>(非交互一次性执行,和桌宠 agent-bridge 一样)
  3. 看 Hermes 有没有真的调到 douyin_watch 插件工具
  4. 看最终输出能不能当桌宠的碎碎念

跑法:
  cd D:/hermes-data/hermes-agent
  venv/Scripts/python.exe D:/hermes-data/plugins/hermes-douyin-pet/tests/test_agent_session.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(r"D:/hermes-data/plugins/hermes-douyin-pet")
HERMES_CMD = r"D:/hermes-data/bin/hermes.cmd"

# ★ 显式指定模型/提供方。
#   实测(2026-11-08):这台机的 config.yaml 里 model.provider=volcengine +
#   api_mode=codex_responses 是硬绑定的,`-m mm` 这种别名**改不掉 provider**,
#   仍然打到火山 → 撞 5 小时配额 429(22:49 重置)。
#   必须同时给 -m <provider>/<model> 和 --provider <provider> 才能切走。
#   桌宠侧 agent-bridge 也有同样的问题(见 electron/agent-bridge.ts)——
#   这个常量就是两边共用的「能跑通的组合」。
MODEL = "minimax-cn/MiniMax-M3.1-Flash"
PROVIDER = "minimax-cn"

# 桌宠侧 electron/douyin-plugin-bridge.ts 里的提示词构造(保持一致)
def build_watch_prompt(max_videos: int = 2, seconds_per_video: int = 5, extra: str = "") -> str:
    parts = [
        "请调用 hermes-douyin-pet 插件的 douyin_watch 工具,让桌宠自己刷抖音。",
        f"参数:max_videos={max_videos}, seconds_per_video={seconds_per_video}。",
        "调用成功后,用一句口语化的话(40 字以内,像在跟主人聊天)总结刚才看到的内容并作为输出。",
        "不要解释你做了什么,不要用列表,只说那一句感想。",
    ]
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def main() -> int:
    print("=" * 66)
    print("★ 桌宠 → Hermes → douyin_watch 插件 真实链路验收")
    print("=" * 66)

    if not Path(HERMES_CMD).is_file():
        print(f"❌ 找不到 hermes CLI:{HERMES_CMD}")
        return 2
    print(f"\nhermes CLI : {HERMES_CMD}")

    prompt = build_watch_prompt(2, 5)
    print("\n=== 桌宠会发的 prompt ===")
    print(prompt)

    env = dict(os.environ)
    # 让子进程拿到 MiniMax key(插件的画面理解要用)
    env_file = Path(r"D:/hermes-data/.env")
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k.startswith("MINIMAX") and k not in env:
                env[k] = v
    print(f"\nMINIMAX key: {'有' if env.get('MINIMAX_CN_API_KEY') else '无'}")

    print("\n=== 跑 hermes -z(非交互一次性,和桌宠 agent-bridge 同一条路) ===")
    t0 = time.time()
    # ★ Windows 上 spawn .cmd 必须走 shell,否则参数根本传不进去。
    #   (和桌宠 electron/agent-bridge.ts 里的 needsShell 判断同一个坑)
    #   实测症状:参数被静默丢掉 → 仍走默认 provider → 撞火山 429。
    cmd_line = ' '.join([
        f'"{HERMES_CMD}"',
        "-z", f'"{prompt}"',
        "-m", MODEL,
        "--provider", PROVIDER,
    ])
    proc = subprocess.Popen(
        cmd_line,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=r"D:/hermes-data/hermes-agent",
        env=env,
    )
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    try:
        out, err = proc.communicate(timeout=900)
        stdout_parts.append(out or "")
        stderr_parts.append(err or "")
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        stdout_parts.append(out or "")
        stderr_parts.append(err or "")
        print("⚠️ 超时(900s)被杀")

    elapsed = time.time() - t0
    stdout = "\n".join(stdout_parts)
    stderr = "\n".join(stderr_parts)

    print(f"\n耗时 {elapsed:.1f}s   exit={proc.returncode}")
    print("\n=== stdout ===")
    print(stdout[-3000:] if stdout else "(空)")
    if stderr.strip():
        print("\n=== stderr(尾部) ===")
        print(stderr[-1500:])

    # ---- 判定:有没有真的调到插件工具 ----
    combined = stdout + "\n" + stderr
    tool_markers = [
        "douyin_watch", "douyin_observe", "douyin_open", "douyin_next",
        "hermes-douyin-pet",
    ]
    hit = [m for m in tool_markers if m in combined]
    print(f"\n=== 判定 ===")
    print(f"工具名出现: {hit if hit else '❌ 一个都没有 —— Hermes 没调插件'}")

    # 最终感想(取 stdout 最后一行非空)
    lines = [l.strip() for l in stdout.splitlines() if l.strip()]
    chatter = lines[-1] if lines else ""
    print(f"最终输出: {chatter[:200]!r}")
    print(f"输出长度: {len(chatter)} 字符(桌宠气泡够不够)")

    ok = bool(hit) and len(chatter) > 0
    print("\n" + "=" * 66)
    print("结果:", "✅ 桌宠 → Hermes → 插件 链路通" if ok else "❌ 链路有问题")
    print("=" * 66)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
