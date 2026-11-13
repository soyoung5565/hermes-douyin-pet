/**
 * 抖音专用浏览器 worker —— **跑在独立 Node 进程里**,管 Playwright 和 Edge。
 *
 * ★ 为什么单独一个文件:
 *   Playwright 必须在真正的 node.exe 下跑(不是 electron.exe),
 *   所以这段代码不在 Electron 主进程里,而是 spawn 出来的子进程。
 *   见 douyin-browser-worker-host.ts 的注释。
 *
 * 通信:stdin/stdout,一行一个 JSON。
 *   读 {"id","type",...} → 做 → 回 {"id","type":"ok","value":...} 或 {"id","type":"error","message":...}
 *   主动事件(state/observe/transcript)不带 id。
 *
 * 用法:node douyin-browser-worker.mjs <profileDir> <port> [playwrightDir]
 *
 * ★ playwrightDir(可选,第 3 个参数):
 *   本文件被复制到 Hermes 插件目录后,`import('playwright-core')` 按 ESM 规则
 *   只在**本文件所在目录向上**找 node_modules —— 找不到就报 MODULE_NOT_FOUND。
 *   宿主(Hermes 源码树 / 桌宠项目)用绝对路径把 playwright-core 的父目录传进来,
 *   这里拼成绝对路径 import,不再依赖 cwd 或目录层级。
 *   兼容旧用法:不传第 3 个参数时仍按普通 ESM 解析(桌宠原项目里 cwd=app/ 就能命中)。
 */

import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';

const profileDir = process.argv[2];
const port = Number(process.argv[3] || 9333);
/** playwright-core 所在目录的**父目录**(即含 node_modules 的那层)。可空。 */
const playwrightRoot = process.argv[4] || '';

if (!profileDir) {
  process.stderr.write('用法:node douyin-browser-worker.mjs <profileDir> <port> [playwrightRoot]\n');
  process.exit(2);
}

/**
 * ★ 跨进程互斥:同一个抖音 profile / CDP 端口同时只能有一个 worker。
 *
 * 为什么要(2026-11-13 实测):两个 session 同时调 douyin_watch 时,后起的 worker
 * 会让先起的 Edge 报 `page.goto: net::ERR_ABORTED`,先起的又报 `worker 没起` ——
 * 两个都失败,用户看到的是「抖音刷不了」。锁文件让第二个直接说清楚原因。
 */
const LOCK_PATH = path.join(profileDir, 'worker.lock');

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function acquireLock() {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const fd = fs.openSync(LOCK_PATH, 'wx');
      fs.writeFileSync(fd, String(process.pid));
      fs.closeSync(fd);
      return;
    } catch {
      let owner = 0;
      try {
        owner = parseInt(fs.readFileSync(LOCK_PATH, 'utf8'), 10) || 0;
      } catch { /* ignore */ }
      // 主人还活着 → 明确拒绝;主人已死 → 清掉锁重试一次
      if (owner && owner !== process.pid && pidAlive(owner)) {
        process.stderr.write(
          `另一个刷抖音任务正在跑(pid=${owner})，等它结束再试。\n`,
        );
        process.exit(3);
      }
      try { fs.unlinkSync(LOCK_PATH); } catch { /* ignore */ }
    }
  }
}

function releaseLock() {
  try {
    const owner = parseInt(fs.readFileSync(LOCK_PATH, 'utf8'), 10) || 0;
    if (!owner || owner === process.pid) fs.unlinkSync(LOCK_PATH);
  } catch { /* ignore */ }
}

/**
 * 抽帧 / 录音的落地目录 —— 必须在 browser-profile 之外(见 observe() 的注释)。
 * 用环境变量覆盖,默认 <hermes-home>/plugin-data/douyin-pet/captures。
 */
function captureRoot() {
  const home = process.env.HERMES_HOME || 'D:/hermes-data';
  const root = path.join(home, 'plugin-data', 'douyin-pet', 'captures');
  fs.mkdirSync(root, { recursive: true });
  return root;
}

/**
 * 加载 playwright-core。
 * 优先用宿主显式指定的根目录;否则退回标准 ESM 解析(本目录向上找 node_modules)。
 *
 * ★ 用 createRequire 而不是 import():
 *   playwright-core 是 CommonJS 包(main 指向 index.js)。用 `import()` 加载 CJS 时,
 *   Node 只保证 default 导出可用 —— 具名导出 `chromium` 可能是 undefined,
 *   表现为 "Cannot destructure property 'chromium' ... as it is undefined"。
 *   require() 走 CJS 互操作,具名导出是稳的。
 */
async function loadChromium() {
  if (playwrightRoot) {
    const req = createRequire(path.join(playwrightRoot, 'noop.js'));
    return req('playwright-core').chromium;
  }
  const { createRequire: cr } = await import('node:module');
  const req = cr(path.join(path.dirname(new URL(import.meta.url).pathname), 'noop.js'));
  return req('playwright-core').chromium;
}

/** 回一条消息。 */
function send(msg) {
  process.stdout.write(JSON.stringify(msg) + '\n');
}

/** 回请求的结果。 */
function reply(id, value) {
  send({ id, type: 'ok', value });
}

/** 回请求的失败。 */
function fail(id, message) {
  send({ id, type: 'error', message: String(message) });
}

/** 主动报错(不带 id)。 */
function reportError(message) {
  send({ type: 'error', message: String(message) });
}

let context = null;
let page = null;
/** Edge 主进程 pid(退出时手动杀,见 startBrowser 的注释)。 */
let edgePid = null;

/**
 * 杀掉 Edge 主进程(连同它的子进程)。
 * 用 taskkill /T —— Edge 是多进程结构,只杀主进程会留下渲染进程。
 */
function killEdge() {
  if (!edgePid) return;
  try {
    if (process.platform === 'win32') {
      spawnSync('taskkill', ['/pid', String(edgePid), '/T', '/F'], { stdio: 'ignore' });
    } else {
      process.kill(edgePid, 'SIGKILL');
    }
  } catch { /* 已经没了就算了 */ }
  edgePid = null;
}

/** 起浏览器。 */
/**
 * 杀掉「占着本 profile」的残留 Edge。
 *
 * ★ 为什么需要(2026-11-13 实测):上一轮崩溃/被 kill 时 Edge 可能活下来,
 *   下一次 launchPersistentContext 起的新进程发现同一个 user-data-dir 已有会话,
 *   会「在已有浏览器会话中打开」然后**自己 exit 0** → Playwright 拿不到浏览器:
 *     browserType.launchPersistentContext: Failed to launch the browser process
 *     [pid=xxxx][out] 在已有浏览器会话中打开。
 *     <process did exit: exitCode=0>
 *   所以起浏览器前先清一遍,失败后再清一遍重试一次。
 */
function killStaleProfileBrowsers() {
  if (process.platform !== 'win32') return;
  const ps =
    `Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | ` +
    `Where-Object { $_.CommandLine -like '*${profileDir}*' } | ` +
    `ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`;
  try {
    spawnSync('powershell', ['-NoProfile', '-Command', ps], {
      stdio: 'ignore', timeout: 15000, windowsHide: true,
    });
  } catch { /* 清理失败不阻塞启动 */ }
  // SingletonLock 是符号链接,进程没了还留着会挡下一次启动
  for (const name of ['SingletonLock', 'SingletonCookie', 'SingletonSocket']) {
    try { fs.rmSync(path.join(profileDir, name), { force: true }); } catch { /* ignore */ }
  }
}

async function launchOnce(chromium) {
  return chromium.launchPersistentContext(profileDir, {
    channel: 'msedge',
    headless: false,
    viewport: null,
    // 不静音 —— 用户要听见视频声音
    ignoreDefaultArgs: ['--mute-audio'],
    args: [
      `--remote-debugging-port=${port}`,
      '--remote-debugging-address=127.0.0.1',
      // 自动播放策略放宽,否则 <video> 播不了
      '--autoplay-policy=no-user-gesture-required',
    ],
  });
}

async function startBrowser() {
  if (context) return;
  // playwright-core 从宿主指定的根目录找(或本目录向上找 node_modules)
  // loadChromium() 返回的就是 chromium 对象本身 —— 别再解构。
  const chromium = await loadChromium();
  fs.mkdirSync(profileDir, { recursive: true });
  killStaleProfileBrowsers();
  try {
    context = await launchOnce(chromium);
  } catch (firstError) {
    // 残留 Edge 抢会话是最常见的失败原因 —— 清干净再试一次,别让整轮白跑
    killStaleProfileBrowsers();
    await sleep(1500);
    try {
      context = await launchOnce(chromium);
    } catch {
      throw firstError;
    }
  }
  const pages = context.pages();
  page = pages.length ? pages[0] : await context.newPage();

  // ★ 记住 Edge 的进程 id —— 退出时要亲手杀掉它。
  //   为什么必须杀:Playwright 启动的 Edge 是 worker 的孙进程,继承了 worker 的
  //   stdout/stderr 管道。worker 退出后 Edge 还活着 → 管道不 EOF →
  //   Hermes 的 `-z` 一直在等 stdout 读完 → **整个命令永远不返回**。
  //   (实测踩过:桌宠调 Hermes 会一直卡住,直到超时。)
  //   实测 msedge 是独立进程组,context.close() 不一定带走它 —— 所以按 pid 杀。
  try {
    const browser = context.browser();
    const proc = browser?.process?.();
    if (proc && proc.pid) {
      edgePid = proc.pid;
      process.stderr.write(`[worker] Edge pid=${edgePid}\n`);
    }
  } catch { /* 拿不到就靠 context.close() 兜底 */ }

  context.on('close', () => {
    context = null;
    page = null;
  });
}

/** 读当前页面状态。 */
async function readState() {
  if (!page) throw new Error('浏览器没起');
  return await page.evaluate(`(() => {
    const v = document.querySelector('video');
    const url = location.href;

    // ★ 作品 ID 的权威来源:激活的 feed item 上的 data-e2e-vid。
    //   探针实测(2026-11-08):推荐流 URL 是 https://www.douyin.com/?recommend=1
    //   —— 从 URL 里正则取 /video/(\\d+) 永远取不到(推荐流本来就不带作品 id)。
    //   真正带 id 的是 [data-e2e="feed-active-video"] 的 data-e2e-vid 属性,
    //   它的 class 里也带 video_<id>。换片后这个值会变 —— 已实测。
    const active = document.querySelector('[data-e2e="feed-active-video"]');
    let videoId = '';
    if (active) {
      videoId = active.getAttribute('data-e2e-vid') || '';
      if (!videoId) {
        const m = /video_(\\d{1,64})/.exec(active.getAttribute('class') || '');
        if (m) videoId = m[1];
      }
    }
    if (!videoId) {
      // 兜底:详情页 URL 形态 https://www.douyin.com/video/<id>
      const m2 = /\\/video\\/(\\d{1,64})/.exec(url);
      if (m2) videoId = m2[1];
    }

    // ★ 标题:og:title 在推荐流里是空的(实测),真实文案在 data-e2e="video-desc"。
    //   换片后这个节点可能还没刷新完 —— 优先取「当前活动作品」内部的文案,
    //   否则会读到上一条的标题(实测:作品 ID 变了、标题还是上一条的)。
    const activeEl = document.querySelector('[data-e2e="feed-active-video"]');
    const descInActive = activeEl ? activeEl.querySelector('[data-e2e="video-desc"]') : null;
    const descEl = descInActive || document.querySelector('[data-e2e="video-desc"], .video-info-detail, h1');
    const desc = descEl ? (descEl.textContent || '').trim() : '';
    const nickEl = (activeEl && activeEl.querySelector('[data-e2e="feed-video-nickname"]'))
      || document.querySelector('[data-e2e="feed-video-nickname"]');
    const nickname = nickEl ? (nickEl.textContent || '').trim() : '';
    const ogTitle = document.querySelector('meta[property="og:title"]')?.content || '';
    const title = (desc || ogTitle || '').slice(0, 200);

    return {
      videoId,
      url,
      title,
      nickname: nickname.slice(0, 80),
      playing: !!v && !v.paused && v.currentTime > 0,
      muted: v ? v.muted : false,
      duration: v ? v.duration : null,
      currentTime: v ? v.currentTime : null,
      // ★ 登录墙:未登录时抖音会压一层「登录后免费畅享高清视频」弹窗,
      //   它盖住推荐流 → 滚轮换片完全不生效(实测:作品 ID 一直不变)。
      //   有弹窗 + 没有活动作品 = 需要人扫码登录。
      loginWall: !!document.querySelector('#login-panel-new, [id^="login-full-panel"]'),
    };
  })()`);
}

/**
 * 关掉抖音的登录弹窗(它盖在推荐流上,滚轮会被它吃掉)。
 * 只点右上角那个关闭 X,不碰登录按钮 —— 绝不代替用户登录。
 */
async function dismissLoginPanel() {
  if (!page) return false;
  try {
    return await page.evaluate(`(() => {
      const panel = document.querySelector('#login-panel-new, [id^="login-full-panel"]');
      if (!panel) return false;
      // 关闭 X:面板右上角的 svg(实测 class 里带 qKr0RhiL 的那种布局)
      let best = null;
      for (const svg of panel.querySelectorAll('svg')) {
        const r = svg.getBoundingClientRect();
        if (r.width < 10 || r.height < 10) continue;
        // 右上角 = x+y 最大
        if (!best || r.x + r.y > best.r.x + best.r.y) best = { svg, r };
      }
      if (!best) return false;
      const el = document.elementFromPoint(best.r.x + best.r.width / 2, best.r.y + best.r.height / 2);
      (el?.closest('svg') || el)?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      return true;
    })()`);
  } catch {
    return false;
  }
}


/** 抽 3 帧 + 录一小段音轨。 */
async function observe() {
  if (!page) throw new Error('浏览器没起');
  // 登录弹窗会盖住画面 —— 抽帧前先关掉(关不掉也不影响截图,只是画面里有弹窗)
  await dismissLoginPanel();
  // ★ 抽帧目录必须在 browser-profile **之外**。
  //   原先写 profileDir/../douyin-state → 落在 <hermes-home>/browser-profile/douyin-state,
  //   而 Hermes 把整个 browser-profile 当「真实浏览器快照(含 cookie/登录态)」保护,
  //   任何工具读那里的文件都会被拦:
  //     "Access denied: ... is inside the Hermes real-profile browser snapshot"
  //   现在放到 <hermes-home>/plugin-data/douyin-pet/captures/ —— 不碰凭据目录。
  const workDir = path.join(captureRoot(), `cap-${Date.now()}`);
  fs.mkdirSync(workDir, { recursive: true });

  const frames = [];
  for (let i = 0; i < 3; i++) {
    try {
      const buf = await page.screenshot({ type: 'jpeg', quality: 80 });
      const fp = path.join(workDir, `f${i}.jpg`);
      fs.writeFileSync(fp, buf);
      frames.push(fp);
    } catch {
      break;
    }
    await sleep(1200);
  }
  return { frames, workDir };
}

/** 录 5 秒音轨(webm/opus),存成文件路径返回。 */
async function captureAudio(workDir) {
  if (!page) return null;
  const b64 = await page.evaluate(`(async () => {
    const v = document.querySelector('video');
    if (!v || v.readyState < 2) return '';
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return '';
    const ctx = new Ctx();
    const src = ctx.createMediaElementSource(v);
    const dest = ctx.createMediaStreamDestination();
    src.connect(dest);
    src.connect(ctx.destination);
    const recorder = new MediaRecorder(dest.stream);
    const chunks = [];
    recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
    recorder.start();
    await new Promise(r => setTimeout(r, 5000));
    recorder.stop();
    const blob = await new Promise(r => recorder.onstop = () => r(new Blob(chunks, { type: 'audio/webm' })));
    ctx.close();
    const buf = await blob.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  })()`);
  if (!b64) return null;
  const raw = path.join(workDir, 'audio.raw');
  fs.writeFileSync(raw, Buffer.from(b64, 'base64'));
  return raw;
}

/**
 * 换下一条。
 *
 * ★ 探针实测结论(2026-11-08,试了 6 种手法):
 *   有效 → Playwright **真实滚轮**(page.mouse.wheel) + 鼠标停在视频区域中央 + 向下滚
 *   无效 → page.evaluate 里造的 wheel 事件(不被页面当用户手势)
 *          键盘 ArrowDown / PageDown
 *          点击 .xgplayer-playswitch 箭头(点得到,但不换片)
 *          改 scrollTop
 *          touch swipe
 *
 *   页面自己的引导文案也印证了这一点:「滚动"鼠标"或点按"键盘上下键"查看更多推荐视频」。
 *   向下滚 = 下一条,向上滚 = 上一条(实测向上滚无效,所以只做下一条)。
 */
async function nextVideo() {
  if (!page) throw new Error('浏览器没起');
  // 登录弹窗会吞掉滚轮 —— 先关掉它
  await dismissLoginPanel();
  const size = page.viewportSize();
  const width = size?.width || 1280;
  const height = size?.height || 800;
  // 鼠标要落在视频区域(左侧偏中),抖音只在这里响应滚轮
  await page.mouse.move(Math.round(width * 0.35), Math.round(height * 0.5));
  await page.mouse.wheel(0, 500);
  // 等切片动画 + 预加载
  await sleep(2500);
  return true;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** 处理一条请求。 */
async function handle(msg) {
  const { id, type } = msg;
  switch (type) {
    case 'start': {
      await startBrowser();
      reply(id, { port });
      break;
    }
    case 'feed': {
      if (!page) throw new Error('浏览器没起');
      await page.goto(msg.url, { waitUntil: 'domcontentloaded', timeout: 45_000 });
      // 抖音是前端渲染的 —— 直接 goto 常拿到空壳,要等 video 元素。
      // 未登录时可能先弹登录墙(它也会挡住 video 的可见性判定),先等"video 或弹窗"其一
      await page.waitForFunction(
        "document.querySelector('video') || document.querySelector('#login-panel-new, [id^=\\\"login-full-panel\\\"]')",
        { timeout: 20_000 },
      );
      await dismissLoginPanel();
      await page.waitForSelector('video', { timeout: 20_000 });
      reply(id, true);
      break;
    }
    case 'state': {
      const state = await readState();
      reply(id, state);
      send({ type: 'state', state });
      break;
    }
    case 'observe': {
      const payload = await observe();
      reply(id, payload);
      send({ type: 'observe', payload });
      break;
    }
    case 'audio': {
      const raw = await captureAudio(msg.workDir || captureRoot());
      reply(id, raw);
      break;
    }
    case 'next': {
      await nextVideo();
      reply(id, true);
      break;
    }
    case 'stop': {
      try {
        await context?.close();
      } catch {
        /* ignore */
      }
      killEdge();
      context = null;
      page = null;
      reply(id, true);
      process.exit(0);
      break;
    }
    default:
      fail(id, `不支持的请求:${type}`);
  }
}

/** 读 stdin 的行。 */
let buffer = '';
process.stdin.on('data', (chunk) => {
  buffer += chunk.toString('utf-8');
  let idx;
  while ((idx = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      reportError('收到非 JSON 行');
      continue;
    }
    handle(msg).catch((e) => {
      if (msg?.id) fail(msg.id, e?.message ?? String(e));
      else reportError(e?.message ?? String(e));
    });
  }
});

// 子进程被杀掉时,把 Edge 也带走(不留孤儿浏览器,也不留孤儿管道)
process.on('SIGTERM', async () => {
  try {
    await context?.close();
  } catch {
    /* ignore */
  }
  killEdge();
  releaseLock();
  process.exit(0);
});
process.on('SIGINT', async () => {
  try {
    await context?.close();
  } catch {
    /* ignore */
  }
  killEdge();
  releaseLock();
  process.exit(0);
});
process.on('exit', releaseLock);

// 启动时先不自动起浏览器 —— 等主进程下 start 指令
acquireLock();
send({ type: 'ready' });
