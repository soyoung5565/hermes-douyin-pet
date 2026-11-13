/**
 * 探针 2:定位「当前这条视频」的唯一 ID 和准确的标题选择器。
 *
 * 探针 1 的发现:
 *   - og:title 是空的 → worker 现在的标题提取对推荐流无效
 *   - data-e2e="video-desc" 有真实文案
 *   - 页面里有一堆 19 位数字,但要确定「哪几个属于当前这条」
 *
 * 目标:找到一个在「换片后一定变化」的 ID 提取方式。
 */

import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs';
import { createRequire } from 'node:module';

const req = createRequire(path.join(process.argv[2] || 'D:/hermes-data/hermes-agent', 'noop.js'));
const { chromium } = req('playwright-core');

const profileDir = path.join(os.tmpdir(), 'dy-probe2-profile');
fs.mkdirSync(profileDir, { recursive: true });

const ctx = await chromium.launchPersistentContext(profileDir, {
  channel: 'msedge',
  headless: false,
  viewport: null,
  ignoreDefaultArgs: ['--mute-audio'],
  args: ['--remote-debugging-port=9356', '--remote-debugging-address=127.0.0.1',
         '--autoplay-policy=no-user-gesture-required'],
});
const page = ctx.pages()[0] ?? await ctx.newPage();
await page.goto('https://www.douyin.com/?recommend=1&from_nav=1', { waitUntil: 'domcontentloaded', timeout: 45000 });
await page.waitForSelector('video', { timeout: 20000 });
await new Promise(r => setTimeout(r, 2500));

/** 读当前条的「指纹」。 */
async function fingerprint(label) {
  const fp = await page.evaluate(`(() => {
    const out = {};
    const desc = document.querySelector('[data-e2e="video-desc"]');
    out.desc = desc ? (desc.textContent || '').trim().slice(0, 120) : '';
    const nickname = document.querySelector('[data-e2e="feed-video-nickname"]');
    out.nickname = nickname ? (nickname.textContent || '').trim().slice(0, 60) : '';
    const h1 = document.querySelector('h1');
    out.h1 = h1 ? (h1.textContent || '').trim().slice(0, 120) : '';

    // 当前激活的 feed item —— 找它的各种 id 属性
    const active = document.querySelector('[data-e2e="feed-active-video"]');
    out.hasActiveItem = !!active;
    out.activeAttrs = {};
    if (active) {
      for (const a of active.getAttributeNames()) {
        const v = String(active.getAttribute(a) || '');
        if (v && v.length < 200) out.activeAttrs[a] = v;
      }
    }
    // 页面里 19 位数字,取前 12
    const html = document.documentElement.innerHTML;
    out.ids = [...new Set((html.match(/\\b\\d{19}\\b/g) || []))].slice(0, 12);

    // 当前 video 元素的最近祖先链上的 id / class
    const v = document.querySelector('video');
    out.videoAncestors = [];
    let el = v;
    for (let i = 0; i < 6 && el; i++) {
      out.videoAncestors.push({
        tag: el.tagName,
        id: el.id || '',
        cls: (el.className && String(el.className).slice(0, 80)) || '',
        dataE2e: el.getAttribute ? (el.getAttribute('data-e2e') || '') : '',
      });
      el = el.parentElement;
    }
    return out;
  })()`);
  console.log(`\n===== ${label} =====`);
  console.log(JSON.stringify(fp, null, 2));
  return fp;
}

const a = await fingerprint('第一条');

// 换片
await page.evaluate(`(() => {
  const next = document.querySelector('[data-e2e="video-switch-next-arrow"], [data-e2e="video-next"], .xg-icon-arrow-right');
  if (next) next.click();
})()`);
await new Promise(r => setTimeout(r, 3000));

const b = await fingerprint('换片后');

// 对比
console.log('\n===== 对比 =====');
console.log('desc 变了:', a.desc !== b.desc);
console.log('ids 集合相同:', JSON.stringify(a.ids) === JSON.stringify(b.ids));
const setA = new Set(a.ids), setB = new Set(b.ids);
console.log('只在第一条:', [...setA].filter(x => !setB.has(x)));
console.log('只在第二条:', [...setB].filter(x => !setA.has(x)));

await ctx.close();
fs.rmSync(profileDir, { recursive: true, force: true });
