/**
 * 探针 3:换片到底怎么才能换成功?
 *
 * 症状(2026-11-08 实测):点 [data-e2e="video-switch-next-arrow"] 之后,
 * readState() 读到的 videoId 没变。但 title 变了(说明确实翻到了别的条目)。
 *
 * 假设:箭头按钮是「滑动式」的 —— 点击只是把焦点移过去,真正切换需要
 *       键盘事件 / 滚轮 / 触摸滑动,或者需要点在正确的子元素上。
 *
 * 这个探针逐一试各种换片手法,每种之后都读一次 videoId,看哪种真换。
 */

import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs';
import { createRequire } from 'node:module';

const req = createRequire(path.join(process.argv[2] || 'D:/hermes-data/hermes-agent', 'noop.js'));
const { chromium } = req('playwright-core');

const profileDir = path.join(os.tmpdir(), 'dy-probe3-profile');
fs.mkdirSync(profileDir, { recursive: true });

const ctx = await chromium.launchPersistentContext(profileDir, {
  channel: 'msedge',
  headless: false,
  viewport: null,
  ignoreDefaultArgs: ['--mute-audio'],
  args: ['--remote-debugging-port=9357', '--remote-debugging-address=127.0.0.1',
         '--autoplay-policy=no-user-gesture-required'],
});
const page = ctx.pages()[0] ?? await ctx.newPage();
await page.goto('https://www.douyin.com/?recommend=1&from_nav=1', { waitUntil: 'domcontentloaded', timeout: 45000 });
await page.waitForSelector('video', { timeout: 20000 });
await new Promise(r => setTimeout(r, 3000));

/** 读当前作品 id + 标题。 */
async function cur() {
  return await page.evaluate(`(() => {
    const active = document.querySelector('[data-e2e="feed-active-video"]');
    let id = '';
    if (active) {
      id = active.getAttribute('data-e2e-vid') || '';
      if (!id) { const m = /video_(\\d{1,64})/.exec(active.getAttribute('class')||''); if (m) id = m[1]; }
    }
    const desc = document.querySelector('[data-e2e="video-desc"]');
    return { id, title: desc ? (desc.textContent||'').trim().slice(0,60) : '' };
  })()`);
}

async function attempt(label, fn) {
  const before = await cur();
  let note = '';
  try { note = await fn(); } catch (e) { note = 'ERR: ' + e.message; }
  await new Promise(r => setTimeout(r, 2500));
  const after = await cur();
  const changed = before.id !== after.id && after.id !== '';
  console.log(`\n[${label}] ${changed ? '✅ 换成功' : '❌ 没换'}`);
  console.log(`   before: ${before.id} | ${before.title}`);
  console.log(`   after : ${after.id} | ${after.title}`);
  if (note) console.log(`   note  : ${note}`);
  return changed;
}

/** 探针:箭头按钮到底长什么样、能不能点。 */
console.log('=== 箭头按钮信息 ===');
console.log(JSON.stringify(await page.evaluate(`(() => {
  const out = {};
  const arrows = document.querySelectorAll('[data-e2e="video-switch-next-arrow"], [data-e2e="video-switch-prev-arrow"]');
  out.count = arrows.length;
  out.list = [...arrows].map(a => ({
    e2e: a.getAttribute('data-e2e'),
    tag: a.tagName,
    cls: (a.className||'').slice(0,100),
    rect: (() => { const r = a.getBoundingClientRect(); return {x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height)}; })(),
    visible: (() => { const r = a.getBoundingClientRect(); return r.width>0 && r.height>0; })(),
    children: [...a.children].map(c => ({tag:c.tagName, cls:(c.className||'').slice(0,60)})).slice(0,4),
  }));
  // 页面提示文案
  out.guide = document.querySelector('[data-e2e="recommend-guide-mask"]')?.textContent?.trim().slice(0,120) || '';
  return out;
})()`), null, 2));

// 手法 1:直接点箭头
await attempt('手法1: 点 next-arrow', () => page.evaluate(`(() => {
  const n = document.querySelector('[data-e2e="video-switch-next-arrow"]');
  if (!n) return 'no button';
  n.click();
  return 'clicked';
})()`));

// 手法 2: 键盘 ArrowDown
await attempt('手法2: 键盘 ArrowDown', async () => {
  await page.keyboard.press('ArrowDown');
  return 'pressed';
});

// 手法 3: 滚轮
await attempt('手法3: 滚轮 down', async () => {
  await page.mouse.wheel(0, 800);
  return 'wheel';
});

// 手法 4: 点箭头的子元素
await attempt('手法4: 点箭头子元素', () => page.evaluate(`(() => {
  const n = document.querySelector('[data-e2e="video-switch-next-arrow"]');
  if (!n) return 'no button';
  const kid = n.querySelector('svg, i, span, div') || n;
  kid.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
  kid.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
  kid.click();
  return 'clicked child';
})()`));

// 手法 5: 派发完整 pointer 事件序列
await attempt('手法5: 完整 pointer 序列', () => page.evaluate(`(() => {
  const n = document.querySelector('[data-e2e="video-switch-next-arrow"]');
  if (!n) return 'no button';
  const r = n.getBoundingClientRect();
  const x = r.x + r.width/2, y = r.y + r.height/2;
  const opts = {bubbles:true, cancelable:true, clientX:x, clientY:y, pointerId:1, isPrimary:true, button:0, buttons:1};
  n.dispatchEvent(new PointerEvent('pointerdown', opts));
  n.dispatchEvent(new PointerEvent('pointerup', {...opts, buttons:0}));
  n.dispatchEvent(new MouseEvent('click', opts));
  return 'pointer seq';
})()`));

// 手法 6: 滑动容器上的触摸事件
await attempt('手法6: touch swipe', () => page.evaluate(`(() => {
  const el = document.querySelector('[data-e2e="slideList"]') || document.querySelector('[id="douyin-right-container"]');
  if (!el) return 'no el';
  const r = el.getBoundingClientRect();
  const y = r.y + r.height/2, x1 = r.x + r.width*0.7, x2 = r.x + r.width*0.3;
  const mk = (type, x) => new TouchEvent(type, {bubbles:true, cancelable:true,
    touches: type==='touchend'?[]:[new Touch({identifier:1, target:el, clientX:x, clientY:y})],
    changedTouches:[new Touch({identifier:1, target:el, clientX:x, clientY:y})]});
  el.dispatchEvent(mk('touchstart', x1));
  el.dispatchEvent(mk('touchmove', (x1+x2)/2));
  el.dispatchEvent(mk('touchend', x2));
  return 'swiped';
})()`));

await ctx.close();
fs.rmSync(profileDir, { recursive: true, force: true });
