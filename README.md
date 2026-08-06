# Animated Coin (with fish)

A spinning gold coin with a fish in the center, on a **transparent background** —
built for compositing into a digital slot machine animation.

## Files
- `coin.html` — the live animation on an HTML `<canvas>`. Transparent background.
  Buttons: Pause/Play, Faster, and **Record .webm** (records a transparent WebM
  in Chromium via `MediaRecorder`).
- `render-mov.mjs` — headless renderer. Drives `coin.html` frame-by-frame and
  muxes the frames into a **transparent ProRes 4444 `.mov`** (real alpha channel).
- `coin.mov` — the exported asset. ProRes 4444, 640×640, 90 frames @ 30fps,
  **seamless loop** (frame 0 == the frame after the last, so it tiles cleanly on
  a slot reel).

## Re-render the .mov
```bash
ln -sf "$(npm root -g)/playwright" node_modules/playwright   # if needed
node render-mov.mjs [frames] [fps]        # defaults: 90 frames, 30 fps
```
Requires a full `ffmpeg` (with the `prores_ks` encoder) on PATH.

## Notes on transparency + format
- A browser cannot natively export a transparent `.mov` — `MediaRecorder` only
  produces WebM/MP4. So the in-page button gives a transparent **WebM**, and the
  true transparent **`.mov`** is produced by the headless renderer here.
- ProRes 4444 alpha is the standard for VFX/game pipelines and imports directly
  into After Effects, Premiere, Nuke, Unity, Unreal, etc.
