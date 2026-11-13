/**
 * 探针 5:用真实 Playwright 交互(坐标点击/真实滚轮)换片。
 *
 * 探针 4 的结论:所有「在 page.evaluate 里造事件」的手法都无效。
 *   原因很可能是抖音的切逻辑绑在**真实用户手势**上(React 合成事件里的
 *   isTrusted=false 会被忽略),或者绑在播放器自己的状态机上。
 *
 * 页面自己的引导文案(实测抓到):
 *   「滚动"鼠标"或点按"键盘上下键"查看更多推荐视频」
 *
 * 所以这次用 Playwright 的真实输入 API:
 *   - page.mouse.click(x, y)   → 真点击(事件带 isTrusted=true)
 *   - page.mouse.wheel()      → 真滚轮
 *   - page.keyboard.press()   → 真按键
 * 而且要在正确的坐标/焦点上。
 */

import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs';
import { createRequire } from 'node:module';

const req = createRequire(path.join(process.argv[2] || 'D:/hermes-data/hermes-agent', 'noop.js'));
const { chromium } = req('playwright-core');

const profileDir = path.join(os.tmpdir(), 'dy-probe5-profile');
fs.mkdirSync(profileDir, { recursive: true });

const ctx = await chromium.launchPersistentContext(profileDir, {
  channel: 'msedge',
  headless: false,
  viewport: null,
  ignoreDefaultArgs: ['--mute-audio'],
  args: ['--remote-debugging-port=9359', '--remote-debugging-address=127.0.0.1',
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
    return { id, title: desc ? (desc.textContent||'').trim().slice(0,44) : '' };
  })()`);
}

async function attempt(label, fn, waitMs = 3500) {
  const before = await cur();
  let note = '';
  try { note = await fn(); } catch (e) { note = 'ERR: ' + e.message; }
  await new Promise(r => setTimeout(r, waitMs));
  const after = await cur();
  const ok = before.id !== after.id && !!after.id;
  console.log(`\n[${label}] ${ok ? '✅ 换了' : '❌ 没换'}`);
  console.log(`   ${before.id || '(空)'} -> ${after.id || '(空)'}`);
  console.log(`   title: ${after.title}`);
  if (note) console.log(`   note: ${note}`);
  return ok;
}

/* 探针:playswitch 元素的信息 */
const ps = await page.evaluate(`(() => {
  const els = [...document.querySelectorAll('[class*="playswitch"], [class*="switch-btn"], [class*="switch"]')];
  return els.map(e => {
    const r = e.getBoundingClientRect();
    return { tag:e.tagName, cls:(e.className||'').toString().slice(0,100), e2e:e.getAttribute('data-e2e')||'',
             rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)},
             visible: r.width>0&&r.height>0,
             html: e.outerHTML.slice(0,200) };
  }).filter(x => x.visible).slice(0, 12);
})()`);
console.log('=== playswitch 元素 ===');
console.log(JSON.stringify(ps, null, 2).slice(0, 3000));

/* 手法 1:真实点击 playswitch 的下箭头 */
const downArrow = await page.evaluate(`(() => {
  const els = [...document.querySelectorAll('.xgplayer-playswitch-tab, .xgplayer-playswitch')];
  const visible = els.filter(e => { const r=e.getBoundingClientRect(); return r.width>0&&r.height>0; });
  if (!visible.length) return null;
  // playswitch 是上下两个 tab,取靠下的
  visible.sort((a,b)=>a.getBoundingClientRect().y - b.getBoundingClientRect().y);
  const el = visible[visible.length-1];
  const r = el.getBoundingClientRect();
  return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2), cls: (el.className||'').toString().slice(0,80) };
})()`);
console.log('\n下箭头目标:', JSON.stringify(downArrow));

if (downArrow) {
  await attempt('手法1: 真坐标点击 playswitch 下箭头', async () => {
    await page.mouse.move(downArrow.x, downArrow.y);
    await page.mouse.down();
    await page.mouse.up();
    return `clicked (${downArrow.x},${downArrow.y})`;
  });
}

/* 手法 2:真实滚轮,鼠标在视频区域中央 */
await attempt('手法2: 真滚轮(视频中心)', async () => {
  const vp = page.viewportSize() || { width: 1280, height: 800 };
  await page.mouse.move(Math.round(vp.width * 0.35), Math.round(vp.height * 0.5));
  await page.mouse.wheel(0, 500);
  return 'wheeled at video center';
});

/* 手法 3:真按键(先点页面聚焦) */
await attempt('手法3: 真按键 ArrowDown', async () => {
  await page.mouse.click(200, 700);   // 先点空白处聚焦 body
  await page.keyboard.press('ArrowDown');
  await new Promise(r => setTimeout(r, 200));
  await page.keyboard.press('ArrowDown');
  return 'pressed ArrowDown x2';
});

/* 手法 4:真按键 PageDown */
await attempt('手法4: 真按键 PageDown', async () => {
  await page.keyboard.press('PageDown');
  return 'pressed PageDown';
});

/* 手法 5:真滚轮向上(某些实现方向相反) */
await attempt('手法5: 真滚轮向上', async () => {
  const vp = page.viewportSize() || { width: 1280, height: 800 };
  await page.mouse.move(Math.round(vp.width * 0.35), Math.round(vp.height * 0.5));
  await page.mouse.wheel(0, -500);
  return 'wheeled up';
});

/* 手法 6: 悬停播放器后点箭头(箭头可能 hover 才显示) */
await attempt('手法6: 悬停视频后找可见箭头再点', async () => {
  const vp = page.viewportSize() || { width: 1280, height: 800 };
  await page.mouse.move(Math.round(vp.width * 0.35), Math.round(vp.height * 0.5));
  await new Promise(r => setTimeout(r, 800));
  const arrow = await page.evaluate(`(() => {
    const all = [...document.querySelectorAll('*')].filter(e => {
      const r = e.getBoundingClientRect();
      if (r.width<12||r.width>70||r.height<12||r.height>90) return false;
      if (r.x < window.innerWidth*0.6) return false;   // 靠右
      const cls = (e.className||'').toString();
      const e2e = e.getAttribute('data-e2e')||'';
      return /arrow|Arrow|switch|Switch|next|prev|down/i.test(cls+e2e);
    });
    if (!all.length) return null;
    const r = all[0].getBoundingClientRect();
    return { x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2),
             cls:(all[0].className||'').toString().slice(0,80), e2e: all[0].getAttribute('data-e2e')||'' };
  })()`);
  if (!arrow) return 'no arrow found';
  await page.mouse.click(arrow.x, arrow.y);
  return 'clicked ' + JSON.stringify(arrow);
});

await ctx.close();
fs.rmSync(profileDir, { recursive: true, force: true });
