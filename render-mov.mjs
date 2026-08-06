// Headless renderer: drives coin.html frame-by-frame, captures each frame as a
// transparent PNG, then muxes them into a transparent ProRes 4444 .mov via ffmpeg.
//
// Usage:  node render-mov.mjs [frames] [fps]
//   frames  number of frames in one full rotation (default 90)  -> seamless loop
//   fps     output frame rate                                    (default 30)
//
// Requires: global `playwright` (run with NODE_PATH=$(npm root -g)) and `ffmpeg`.

import { chromium } from "playwright";
import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dir = dirname(fileURLToPath(import.meta.url));
const FRAMES = parseInt(process.argv[2] || "90", 10);
const FPS = parseInt(process.argv[3] || "30", 10);
const OUT_DIR = join(__dir, "frames");
const MOV = join(__dir, "coin.mov");

rmSync(OUT_DIR, { recursive: true, force: true });
mkdirSync(OUT_DIR, { recursive: true });

const browser = await chromium.launch();
// deviceScaleFactor bumps the backing store for extra-crisp frames.
const page = await browser.newPage({ viewport: { width: 640, height: 640 }, deviceScaleFactor: 1 });
await page.goto("file://" + join(__dir, "coin.html"));
await page.evaluate(() => { window.__headless = true; }); // stop the rAF auto-spin
await page.waitForFunction(() => typeof window.drawAngle === "function");

for (let i = 0; i < FRAMES; i++) {
  const angle = (i / FRAMES) * Math.PI * 2; // last frame stops just before 2π -> seamless
  const dataURL = await page.evaluate((a) => {
    window.drawAngle(a);
    return document.getElementById("coin").toDataURL("image/png"); // keeps alpha
  }, angle);
  const b64 = dataURL.replace(/^data:image\/png;base64,/, "");
  writeFileSync(join(OUT_DIR, `f${String(i).padStart(4, "0")}.png`), Buffer.from(b64, "base64"));
}
await browser.close();
console.log(`Captured ${FRAMES} transparent PNG frames.`);

// ProRes 4444 carries a real alpha channel and is the standard for VFX/game
// pipelines. yuva444p10le = 10-bit 4:4:4 + alpha.
const args = [
  "-y",
  "-framerate", String(FPS),
  "-i", join(OUT_DIR, "f%04d.png"),
  "-c:v", "prores_ks",
  "-profile:v", "4444",
  "-pix_fmt", "yuva444p10le",
  "-vendor", "apl0",
  MOV,
];
const r = spawnSync("ffmpeg", args, { stdio: "inherit" });
if (r.status !== 0) process.exit(r.status || 1);
console.log("Wrote " + MOV);
