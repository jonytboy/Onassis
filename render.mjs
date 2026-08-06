// Renders the coin animations to game-ready assets:
//   * transparent ProRes 4444 .mov      (compositing / video pipelines)
//   * sprite-sheet PNG atlas            (Phaser / Unity / PixiJS)
//   * JSON atlas (Phaser JSONHash)      (frame rects + animation tag)
//
// One page drives everything via window.drawSpin(i,N) / window.drawBurst(i,N).
//
// Usage: node render.mjs
// Requires: global `playwright` symlinked into ./node_modules, and full `ffmpeg`.

import { chromium } from "playwright";
import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dir = dirname(fileURLToPath(import.meta.url));

// ---- what to build -------------------------------------------------------
const METALS = ["gold"];                 // add "silver","bronze" to output more
const CELL = 160;                        // sprite-sheet cell size (px)
const FPS = 30;
const CLIPS = [
  { name: "spin",  fn: "drawSpin",  frames: 90, cols: 10, rows: 9, loop: true,  previewLoops: 3 },
  { name: "burst", fn: "drawBurst", frames: 72, cols: 9,  rows: 8, loop: false, previewLoops: 2 },
];
const PREVIEW_BG = "0x14141f";           // dark reel-style bg for the .mp4 previews

const ff = (args) => {
  const r = spawnSync("ffmpeg", ["-y", ...args], { stdio: ["ignore", "ignore", "inherit"] });
  if (r.status !== 0) throw new Error("ffmpeg failed: " + args.join(" "));
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 640, height: 640 } });
await page.goto("file://" + join(__dir, "coin.html"));
await page.evaluate(() => { window.__headless = true; });
await page.waitForFunction(() => typeof window.drawSpin === "function" && typeof window.drawBurst === "function");

const made = [];

for (const metal of METALS) {
  await page.evaluate((m) => window.setMetal(m), metal);
  const tag = (base) => (METALS.length > 1 ? `${base}_${metal}` : base);

  for (const clip of CLIPS) {
    const dir = join(__dir, `_frames_${metal}_${clip.name}`);
    rmSync(dir, { recursive: true, force: true });
    mkdirSync(dir, { recursive: true });

    for (let i = 0; i < clip.frames; i++) {
      const dataURL = await page.evaluate(({ fn, i, N }) => {
        window[fn](i, N);
        return document.getElementById("coin").toDataURL("image/png");
      }, { fn: clip.fn, i, N: clip.frames });
      const b64 = dataURL.replace(/^data:image\/png;base64,/, "");
      writeFileSync(join(dir, `f${String(i).padStart(4, "0")}.png`), Buffer.from(b64, "base64"));
    }

    // 1) transparent ProRes 4444 .mov
    const mov = join(__dir, `${tag("coin_" + clip.name)}.mov`);
    ff(["-framerate", String(FPS), "-i", join(dir, "f%04d.png"),
        "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-vendor", "apl0", mov]);
    made.push(mov);

    // 1b) transparent WebM (VP9 + alpha) — browser-playable, from the alpha PNGs
    const webm = join(__dir, `${tag("coin_" + clip.name)}.webm`);
    ff(["-framerate", String(FPS), "-i", join(dir, "f%04d.png"),
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "26",
        "-auto-alt-ref", "0", "-an", webm]);
    made.push(webm);

    // 1c) MP4 preview (H.264 over a dark bg) — plays in any player/browser
    const mp4 = join(__dir, `${tag("coin_" + clip.name)}_preview.mp4`);
    ff(["-stream_loop", String(clip.previewLoops - 1), "-i", mov,
        "-f", "lavfi", "-i", `color=c=${PREVIEW_BG}:s=640x640:r=${FPS}`,
        "-filter_complex", "[1][0]overlay=shortest=1:format=auto,format=yuv420p",
        "-c:v", "libx264", "-crf", "20", "-movflags", "+faststart", mp4]);
    made.push(mp4);

    // 2) sprite sheet (uniform grid, transparent)
    const sheet = join(__dir, `${tag("coin_" + clip.name)}_sheet.png`);
    ff(["-i", join(dir, "f%04d.png"),
        "-vf", `scale=${CELL}:${CELL},tile=${clip.cols}x${clip.rows}`,
        "-frames:v", "1", sheet]);
    made.push(sheet);

    // 3) JSON atlas (Phaser JSONHash) + a uniform-grid hint for Unity
    const frames = {};
    for (let i = 0; i < clip.frames; i++) {
      const col = i % clip.cols, rowi = Math.floor(i / clip.cols);
      frames[`${clip.name}_${String(i).padStart(2, "0")}`] = {
        frame: { x: col * CELL, y: rowi * CELL, w: CELL, h: CELL },
        rotated: false, trimmed: false,
        spriteSourceSize: { x: 0, y: 0, w: CELL, h: CELL },
        sourceSize: { w: CELL, h: CELL },
      };
    }
    const atlas = {
      frames,
      meta: {
        app: "coin-render", version: "1.0",
        image: `${tag("coin_" + clip.name)}_sheet.png`,
        format: "RGBA8888",
        size: { w: clip.cols * CELL, h: clip.rows * CELL },
        scale: "1",
        grid: { cell: CELL, cols: clip.cols, rows: clip.rows, count: clip.frames },
        animation: { name: clip.name, fps: FPS, loop: clip.loop, frameCount: clip.frames },
      },
    };
    const jsonPath = join(__dir, `${tag("coin_" + clip.name)}.json`);
    writeFileSync(jsonPath, JSON.stringify(atlas, null, 2));
    made.push(jsonPath);

    rmSync(dir, { recursive: true, force: true });
    console.log(`built ${clip.name} (${metal}): mov + webm + mp4 + sheet + json`);
  }
}

await browser.close();
console.log("\nArtifacts:\n" + made.map((f) => "  " + f.replace(__dir + "/", "")).join("\n"));
