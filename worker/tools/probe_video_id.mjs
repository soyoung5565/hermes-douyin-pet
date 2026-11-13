/**
 * 探针:抖音推荐流里作品 ID 到底在哪。
 *
 * 背景:worker's readState() 只按 `/video/(\d+)` 正则从 URL 里取 ID,
 * 但推荐流的 URL 是 `https://www.douyin.com/?recommend=1` —— 取不到,
 * 于是 videoId 恒为空(实测已复现)。作品 ID 其实在 DOM 里。
 *
 * 这个探针把页面上所有可能的 ID 来源都打出来,看哪个真的有值。
 */

import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs';
import { createRequire } from 'node:module';

// playwright-core 是 CJS 包,ESM 的 import() 只保证 default 导出 —— 用 require 拿具名导出。
const req = createRequire(path.join(process.argv[2] || 'D:/hermes-data/hermes-agent', 'noop.js'));
const { chromium } = req('playwright-core');

const profileDir = path.join(os.tmpdir(), 'dy-probe-profile');
fs.mkdirSync(profileDir, { recursive: true });

const ctx = await chromium.launchPersistentContext(profileDir, {
  channel: 'msedge',
  headless: false,
  viewport: null,
  ignoreDefaultArgs: ['--mute-audio'],
  args: ['--remote-debugging-port=9355', '--remote-debugging-address=127.0.0.1',
         '--autoplay-policy=no-user-gesture-required'],
});
const page = ctx.pages()[0] ?? await ctx.newPage();
await page.goto('https://www.douyin.com/?recommend=1&from_nav=1', { waitUntil: 'domcontentloaded', timeout: 45000 });
await page.waitForSelector('video', { timeout: 20000 });
await new Promise(r => setTimeout(r, 2500));

const info = await page.evaluate(`(() => {
  const out = {};
  // 1. URL
  out.url = location.href;
  // 2. 各种 [data-e2e] 属性
  out.dataE2e = {};
  document.querySelectorAll('[data-e2e]').forEach(el => {
    const k = el.getAttribute('data-e2e');
    if (k && !out.dataE2e[k]) out.dataE2e[k] = (el.textContent || '').trim().slice(0, 80);
  });
  // 3. 页面里所有 19 位数字(抖音作品 id 是 19 位)
  const html = document.documentElement.innerHTML;
  const ids = new Set((html.match(/\\b\\d{19}\\b/g) || []).slice(0, 40));
  out.nineteenDigitIds = [...ids];
  // 4. 链接里的 /video/xxx
  out.videoLinks = [...document.querySelectorAll('a[href*="/video/"]')].slice(0, 8).map(a => a.getAttribute('href'));
  // 5. 播放器容器上的属性
  out.playerAttrs = {};
  const player = document.querySelector('[data-e2e="video-player"], .xg-player-container, [id^="player"]');
  if (player) {
    for (const attr of player.getAttributeNames()) out.playerAttrs[attr] = String(player.getAttribute(attr)).slice(0, 120);
  }
  // 6. 标题
  out.ogTitle = document.querySelector('meta[property="og:title"]')?.content || '';
  out.ogUrl = document.querySelector('meta[property="og:url"]')?.content || '';
  out.ogVideo = document.querySelector('meta[property="og:video"]')?.content || '';
  out.ogDescription = (document.querySelector('meta[property="og:description"]')?.content || '').slice(0, 200);
  // 7. 隐藏的 script 里的 aweme id
  out.awemeIds = [...new Set((html.match(/aweme_id["\\s:]+["']?(\\d{19})/g) || []).slice(0, 10))];
  // 8. 视频源
  const v = document.querySelector('video');
  out.videoSrc = v ? (v.currentSrc || v.src || '').slice(0, 200) : '';
  out.videoDur = v ? v.duration : null;
  out.videoTime = v ? v.currentTime : null;
  return out;
})()`);

console.log(JSON.stringify(info, null, 2));

await ctx.close();
fs.rmSync(profileDir, { recursive: true, force: true });
