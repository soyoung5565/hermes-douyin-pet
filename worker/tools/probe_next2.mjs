/**
 * 探针 4:找到真正能换片的控件。
 *
 * 探针 3 的结论:
 *   - [data-e2e="video-switch-next-arrow"] 在 DOM 里 count=0(不存在)
 *   - 键盘 ArrowDown 无效、滚轮无效、touch swipe 无效
 *   - 但「点一个不存在的按钮」却让 ID 变了 —— 那是**预加载**效果,
 *     页面本来就在后台加载下一条,ID 变化是异步的,不是点击的功劳。
 *
 * 也就是说:探针 3 的「✅ 换成功」是假阳性。
 *
 * 现在要找:真正驱动切片的控件在哪。可能的方向:
 *   a) 页面上的箭头图标(不是 data-e2e,是 class)
 *   b) 侧边栏的上下箭头
 *   c) 内部状态管理(redux store / 事件总线)
 *   d) 键盘要在特定元素聚焦时
 *   e) 直接改 scrollTop 触发 IntersectionObserver
 */

import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs';
import { createRequire } from 'node:module';

const req = createRequire(path.join(process.argv[2] || 'D:/hermes-data/hermes-agent', 'noop.js'));
const { chromium } = req('playwright-core');

const profileDir = path.join(os.tmpdir(), 'dy-probe4-profile');
fs.mkdirSync(profileDir, { recursive: true });

const ctx = await chromium.launchPersistentContext(profileDir, {
  channel: 'msedge',
  headless: false,
  viewport: null,
  ignoreDefaultArgs: ['--mute-audio'],
  args: ['--remote-debugging-port=9358', '--remote-debugging-address=127.0.0.1',
         '--autoplay-policy=no-user-gesture-required'],
});
const page = ctx.pages()[0] ?? await ctx.newPage();
await page.goto('https://www.douyin.com/?recommend=1&from_nav=1', { waitUntil: 'domcontentloaded', timeout: 45000 });
await page.waitForSelector('video', { timeout: 20000 });
await new Promise(r => setTimeout(r, 4000));

async function cur() {
  return await page.evaluate(`(() => {
    const active = document.querySelector('[data-e2e="feed-active-video"]');
    let id = '';
    if (active) {
      id = active.getAttribute('data-e2e-vid') || '';
      if (!id) { const m = /video_(\\d{1,64})/.exec(active.getAttribute('class')||''); if (m) id = m[1]; }
    }
    const desc = document.querySelector('[data-e2e="video-desc"]');
    return { id, title: desc ? (desc.textContent||'').trim().slice(0,50) : '' };
  })()`);
}

/** 列出所有看起来像「箭头/切换」的可点元素。 */
console.log('=== 候选换片控件 ===');
const candidates = await page.evaluate(`(() => {
  const out = [];
  const sel = 'button, [role="button"], [class*="arrow"], [class*="Arrow"], [class*="switch"], [class*="Switch"], svg[class*="arrow"], [class*="icon-"], [data-e2e*="switch"], [data-e2e*="arrow"], [data-e2e*="next"], [data-e2e*="prev"]';
  document.querySelectorAll(sel).forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    out.push({
      tag: el.tagName,
      e2e: el.getAttribute('data-e2e') || '',
      cls: (el.className && typeof el.className === 'string' ? el.className : (el.getAttribute('class')||'')).slice(0, 90),
      text: (el.textContent || '').trim().slice(0, 30),
      rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
    });
  });
  return out;
})()`);
console.log(JSON.stringify(candidates, null, 2).slice(0, 4000));

/** 检查是否有内部滚动容器。 */
console.log('\n=== 可滚动容器 ===');
console.log(JSON.stringify(await page.evaluate(`(() => {
  const out = [];
  document.querySelectorAll('*').forEach(el => {
    if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 200) {
      out.push({ tag: el.tagName, id: el.id||'', cls: (el.className||'').toString().slice(0,80),
                 scrollTop: el.scrollTop, scrollH: el.scrollHeight, clientH: el.clientHeight });
    }
  });
  return out.slice(0, 10);
})()`), null, 2));

/** 试 window 上的事件总线 / 全局 store。 */
console.log('\n=== 全局对象探测 ===');
console.log(JSON.stringify(await page.evaluate(`(() => {
  const keys = Object.keys(window).filter(k => /store|redux|zustand|bus|event|__|router/i.test(k)).slice(0, 40);
  return { keys };
})()`), null, 2));

async function attempt(label, fn, waitMs = 3000) {
  const before = await cur();
  let note = '';
  try { note = await fn(); } catch (e) { note = 'ERR: ' + e.message; }
  await new Promise(r => setTimeout(r, waitMs));
  const after = await cur();
  console.log(`\n[${label}] ${before.id !== after.id && after.id ? '✅ 换了' : '❌ 没换'}`);
  console.log(`   ${before.id} -> ${after.id}`);
  if (note) console.log(`   note: ${note}`);
  return before.id !== after.id && !!after.id;
}

/** A: 点击侧边箭头(按 class 找 svg 箭头) */
await attempt('A: 按 class 找箭头 svg', () => page.evaluate(`(() => {
  const cands = [...document.querySelectorAll('svg')].filter(s => {
    const r = s.getBoundingClientRect();
    return r.width>10 && r.width<60 && r.height>10 && r.height<60 && (s.className||'').toString().match(/arrow|Arrow|next|prev|switch/i);
  });
  if (!cands.length) return 'no svg candidates';
  // 挑 x 靠右的(下一个)
  cands.sort((a,b)=>a.getBoundingClientRect().x - b.getBoundingClientRect().x);
  const target = cands[cands.length-1];
  const r = target.getBoundingClientRect();
  target.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX:r.x+r.width/2, clientY:r.y+r.height/2}));
  return 'clicked svg at x=' + Math.round(r.x);
})()`));

/** B: 改滚动容器的 scrollTop */
await attempt('B: 改 scrollTop', () => page.evaluate(`(() => {
  let el = null;
  document.querySelectorAll('*').forEach(e => {
    if (!el && e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 200) el = e;
  });
  if (!el) return 'no scroll container';
  el.scrollTop = el.scrollTop + el.clientHeight;
  return 'scrolled to ' + el.scrollTop + '/' + el.scrollHeight;
})()`));

/** C: 页面自己往哪个方向滚? 监听 scroll 事件看真实滚动容器 */
console.log('\n=== 真实滚动容器探测(监听 scroll) ===');
await page.evaluate(`(() => {
  window.__scrolls = [];
  document.addEventListener('scroll', (e) => {
    const t = e.target;
    if (t === document || t === window) { window.__scrolls.push({who:'window', top: window.scrollY}); return; }
    window.__scrolls.push({who: (t.id||t.className||t.tagName).toString().slice(0,60), top: t.scrollTop, h: t.scrollHeight, ch: t.clientHeight});
  }, true);
})()`);
await attempt('C: 触发 wheel 后看 scroll 事件', async () => {
  await page.mouse.move(600, 400);
  await page.mouse.wheel(0, 600);
  await new Promise(r => setTimeout(r, 800));
  const logs = await page.evaluate(`window.__scrolls.slice(-10)`);
  console.log('   scroll 事件:', JSON.stringify(logs));
  return 'wheeled';
});

/** D: 点击「点赞」旁的箭头? 先看页面右侧结构 */
console.log('\n=== 右侧操作栏结构 ===');
console.log(JSON.stringify(await page.evaluate(`(() => {
  const bar = document.querySelector('[data-e2e="video-player-digg"]')?.closest('div[class*="video"]')?.parentElement
    || document.querySelector('#douyin-right-container');
  if (!bar) return 'no bar';
  return [...bar.querySelectorAll('div')].slice(0,20).map(d => ({
    cls: (d.className||'').toString().slice(0,70),
    e2e: d.getAttribute('data-e2e')||'',
    txt: (d.textContent||'').trim().slice(0,20),
  }));
})()`), null, 2).slice(0, 2500));

await ctx.close();
fs.rmSync(profileDir, { recursive: true, force: true });
