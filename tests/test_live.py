"""插件真跑验证 —— 真的开浏览器、真的进抖音、真的抽帧送模型。

不 mock、不打桩:这个脚本调的就是插件里 agent 会调的那些 handler。

跑法:
  cd D:/hermes-data/hermes-agent
  venv/Scripts/python.exe D:/hermes-data/plugins/hermes-douyin-pet/tests/test_live.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(r"D:/hermes-data/plugins/hermes-douyin-pet")
sys.path.insert(0, str(PLUGIN_DIR.parent))

# 插件的相对 import 需要包上下文
import importlib.util

spec = importlib.util.spec_from_file_location(
    "hermes_douyin_pet",
    PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
pkg = importlib.util.module_from_spec(spec)
sys.modules["hermes_douyin_pet"] = pkg
spec.loader.exec_module(pkg)

from hermes_douyin_pet import tools as T  # noqa: E402
from hermes_douyin_pet import worker_bridge as WB  # noqa: E402


class FakeCtx:
    """最小 ctx —— 只提供插件用到的 llm.complete / llm.complete_structured。

    这里**不用真模型**跑冒烟测试(那要花钱且慢),而是把模型输入原样打出来,
    证明「抽帧真的送到了模型面前」。真正的模型理解在 Hermes 里跑。
    """

    def __init__(self):
        self.llm = FakeLlm()


class FakeLlm:
    def complete(self, messages=None, **kwargs):
        print("   [llm.complete] 输入:", json.dumps(messages, ensure_ascii=False)[:300])
        return type("R", (), {"text": "还行吧,这条我看了三秒就划走了。", "parsed": None})()

    def complete_structured(self, instructions=None, input=None, **kwargs):
        images = [b for b in (input or []) if isinstance(b, dict) and b.get("type") == "image"]
        texts = [b.get("text", "") for b in (input or []) if isinstance(b, dict) and b.get("type") == "text"]
        total_bytes = sum(len(b.get("data") or b"") for b in images)
        print(f"   [llm.complete_structured] 图像块={len(images)} 总字节={total_bytes}")
        for t in texts:
            print("   [llm text]", t[:200].replace("\n", " | "))
        parsed = {
            "visual": "一只橘猫在纸箱里打滚",
            "worth": "yes",
            "reason": "猫很可爱",
        }
        return type("R", (), {"text": json.dumps(parsed, ensure_ascii=False), "parsed": parsed})()


def main() -> int:
    print("=" * 60)
    print("hermes-douyin-pet 插件 · 真实链路验证")
    print("=" * 60)

    print("\n[0] 依赖检查")
    print("   node      :", WB.find_node())
    print("   edge      :", WB.find_edge())
    print("   playwright:", WB.find_playwright_root())
    if not T.check_douyin_requirements():
        print("❌ 依赖不满足:", T.requirements_hint())
        return 2
    print("   ✅ 依赖齐")

    ctx = FakeCtx()
    ok = True

    try:
        print("\n[1] douyin_open —— 起专用 Edge + 进推荐流")
        t0 = time.time()
        out = json.loads(T.handle_douyin_open({}, ctx=ctx))
        print(f"   ({time.time() - t0:.1f}s)")
        print("   ", json.dumps(out, ensure_ascii=False)[:400])
        if not out.get("success"):
            ok = False

        print("\n[2] douyin_status —— 读页面状态")
        out = json.loads(T.handle_douyin_status({}, ctx=ctx))
        print("   ", json.dumps(out, ensure_ascii=False)[:400])
        if not out.get("success"):
            ok = False

        print("\n[3] douyin_observe —— 抽帧 + 录音 + 送模型")
        t0 = time.time()
        out = json.loads(T.handle_douyin_observe({"seconds": 3}, ctx=ctx))
        print(f"   ({time.time() - t0:.1f}s)")
        print("   ", json.dumps(out, ensure_ascii=False)[:500])
        if not out.get("success"):
            ok = False

        print("\n[4] douyin_next —— 换下一条")
        out = json.loads(T.handle_douyin_next({}, ctx=ctx))
        print("   ", json.dumps(out, ensure_ascii=False)[:300])

        print("\n[5] douyin_watch —— ★ 自主刷视频主循环(2 条)")
        t0 = time.time()
        out = json.loads(T.handle_douyin_watch({"max_videos": 2, "seconds_per_video": 5}, ctx=ctx))
        print(f"   ({time.time() - t0:.1f}s)")
        print("   count   =", out.get("count"))
        print("   summary =", out.get("summary"))
        for item in out.get("watched", []):
            print(f"     #{item.get('index')} id={item.get('videoId')} visual={item.get('visual')} err={item.get('error')}")
        if out.get("count", 0) < 1:
            print("   ❌ 一条都没看成")
            ok = False

    finally:
        print("\n[6] 收尾:关浏览器")
        out = json.loads(T.handle_douyin_close({}, ctx=ctx))
        print("   ", json.dumps(out, ensure_ascii=False)[:200])
        # 确保 worker 进程真的退了
        worker = WB._worker
        if worker is not None:
            worker.stop()
        print("   ✅ 已关")

    print("\n" + "=" * 60)
    print("结果:", "✅ 全绿" if ok else "❌ 有失败")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
