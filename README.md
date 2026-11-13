# hermes-douyin-pet —— 让 Hermes 桌宠自己刷抖音

让 Hermes 用一个专用 Microsoft Edge 实例自己刷抖音：抽真实画面 → 让宿主模型判断值不值得看
→ 换片 / 继续 → 最后用一句话说出感想。桌宠（`D:\hsr-pet`）通过 Hermes CLI 调这个插件。

实现方式参考上游 [LockyBee/dsh-douyin-agent](https://github.com/LockyBee/dsh-douyin-agent)（MIT）：
**浏览器控制在 Node 侧（Playwright 驱动系统 Edge + 持久 profile 存登录态），上层调度在宿主侧**
（原项目宿主是 DSH，这里宿主是 Hermes）。

> 上游那些跟 QQ 私信、公开评论、自主分享、好友许可名单深度耦合的能力**没有搬** ——
> 桌宠场景不需要，搬过来只会引入一堆没法验证的外部副作用。

---

## 架构

```
桌宠 (Electron)
  │  agent-bridge: spawn cmd.exe /c hermes.cmd -m <model> --provider <p> -z "<prompt>"
  ▼
Hermes (宿主)
  │  agent 自己选到 douyin_watch 工具
  ▼
hermes-douyin-pet (本插件)
  │  Python 薄壳 (worker_bridge.py) ──stdin/stdout JSON 协议──> Node worker
  │                                                                     │
  │                                                                     ▼
  │                                          Playwright → 系统 Edge(专用实例,持久 profile)
  └─ 画面理解:ctx.llm.complete_structured(input=[image blocks])  ← 宿主当前模型
```

**模型能力走宿主**（`ctx.llm`）：插件自己不绑 API key。用户在 Hermes 里换模型，刷视频的判断力跟着变。
这与原项目在 DSH 里用宿主模型是同一种形态。

---

## 暴露的工具

| 工具 | 作用 |
|---|---|
| `douyin_open` | 打开推荐流（第一次可能需要用户手动在 Edge 里登录） |
| `douyin_status` | 读当前页面 / 作品 ID / 标题 / 播放状态 |
| `douyin_observe` | 抽 3 帧 + 录 5 秒音轨 → 让模型理解画面与对白 |
| `douyin_next` | 换下一条（真实滚轮手势） |
| `douyin_play` / `douyin_pause` | 播放 / 暂停 |
| `douyin_watch` | ★ **自主刷视频主入口**（抽帧→理解→判断→换片→说感想） |
| `douyin_close` | 关闭专用浏览器 |

---

## 依赖

| 依赖 | 说明 | 缺了会怎样 |
|---|---|---|
| Node.js | 跑浏览器 worker | 插件不可用 |
| `playwright-core` | 驱动 Edge | 插件不可用（Hermes 源码树自带） |
| Microsoft Edge | 被驱动的浏览器 | 插件不可用（用 `channel:'msedge'`，不下载 Chromium） |
| `MINIMAX_CN_API_KEY` | **可选**，只用于音轨转写（ASR） | 不配也能刷，只是听不见对白 |

插件会自己找这些依赖：`$HERMES_HOME/hermes-agent/node_modules`（playwright）、`PATH` / Hermes 自带 node、
`Program Files (x86)/Microsoft/Edge`。

---

## 实测结论（2026-11-08，全部在本机跑过）

### 抖音推荐流的真实结构

| 要找的东西 | 在哪 | 踩过的坑 |
|---|---|---|
| **作品 ID** | `[data-e2e="feed-active-video"]` 的 `data-e2e-vid` 属性 | 从 URL 取 `/video/(\d+)` **永远取不到** —— 推荐流 URL 是 `?recommend=1`，本来就不带作品 id |
| **标题 / 文案** | `[data-e2e="video-desc"]` 的 textContent | `og:title` 在推荐流里**是空的**（实测），旧代码优先读它 → 标题全空 |
| **作者昵称** | `[data-e2e="feed-video-nickname"]` | — |
| **换片** | Playwright **真实滚轮**（`page.mouse.wheel(0, 500)`），鼠标停在视频区域中央 | 见下 |

### 换片：只有一种手法有效（试了 6 种）

| 手法 | 结果 |
|---|---|
| ✅ `page.mouse.wheel(0, 500)`，鼠标在视频区域中央 | **有效** |
| ❌ `page.evaluate` 里造 wheel 事件 | 无效（不被当用户手势） |
| ❌ 键盘 ArrowDown / PageDown | 无效 |
| ❌ 点击 `.xgplayer-playswitch` 箭头（点得到） | 无效（只切换焦点，不换片） |
| ❌ 改 `scrollTop` | 无效 |
| ❌ touch swipe | 无效 |

抖音自己的引导文案印证了这点：「滚动"鼠标"或点按"键盘上下键"查看更多推荐视频」。向下滚 = 下一条。

### Playwright 加载：必须用 `createRequire`，不能用 `import()`

`playwright-core` 是 CommonJS 包。ESM 的 `import()` 对 CJS **只保证 default 导出** ——
具名导出 `chromium` 可能是 `undefined`，表现为
`Cannot destructure property 'chromium' of '(intermediate value)' as it is undefined`。
用 `createRequire(path.join(playwrightRoot, 'noop.js'))('playwright-core').chromium` 才稳。

另：ESM 的 `import` 只在**文件所在目录向上**找 `node_modules`。插件目录离 Hermes 源码树很远，
所以宿主要把 playwright-core 的父目录**绝对路径**传给 worker（worker 的第 3 个参数）。

### 抽帧送模型

`ctx.llm.complete_structured(input=[{type:'image', data:<bytes>, mime_type:'image/jpeg'}, ...])`
—— 对应上游的 `projectFrames`。实测一次 3 帧约 200-700 KB，走得通。

### 音轨转写（可选）

Hermes 核心**没有暴露 ASR 接口**（整个源码树没有 `speech_to_text` 调用点），所以音轨转写走
MiniMax `/v1/speech_to_text`（multipart，`model=asr-1.0`，**只收 wav**）。
录音是 webm/opus，要先用 ffmpeg 转 16k 单声道 wav（ffmpeg 在 `AppData/Local/hermes/tools/ffmpeg-*/bin/`）。
**没配 MiniMax key 时直接跳过转写，刷视频主流程不受影响。**

---

## 验证脚本

```bash
cd D:/hermes-data/hermes-agent

# 1. 插件自身的真实链路(开 Edge、进抖音、抽帧、送模型、换片)
venv/Scripts/python.exe D:/hermes-data/plugins/hermes-douyin-pet/tests/test_live.py

# 2. 桌宠 → Hermes → 插件 全链路(Node 版,复刻桌宠 agent-bridge 的调用方式)
cd D:/hsr-pet/Desktop-pet/app && npx tsx scripts/test_douyin_plugin_chain.ts
```

`worker/probe_*.mjs` 是摸清抖音 DOM 结构的调试脚本（2026-11-08 实测记录），
不是运行时依赖 —— 改 worker 的选择器时重跑它们对照。

---

## 安装 / 启用

插件已经在 `D:\hermes-data\plugins\hermes-douyin-pet`，并已启用：

```bash
cd D:/hermes-data/hermes-agent
venv/Scripts/python.exe -m hermes_cli.main plugins enable hermes-douyin-pet
```

启用后工具在**下一个会话**生效（`plugins enable` 的输出会提示 `tools (next session)`）。

---

## 已知限制

- **音轨转写要额外配 MiniMax key**，没配就只看画面。
- 第一次使用需要在弹出的 Edge 窗口里**手动登录抖音**（持久 profile，之后免登）。
- 火山方舟的 Agent Plan 是 **5 小时配额**，烧满会 429。桌宠侧默认显式指定
  `minimax-cn/MiniMax-M3.1-Flash` 绕开（见 `D:\hsr-pet` 的 `DEFAULT_AGENT_BRIDGE`）。
