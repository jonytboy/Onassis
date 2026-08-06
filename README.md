# Animated Coin — spin & win-burst (for a digital slot machine)

A gold coin with a refined fish emblem, on a **transparent background**, in two
animations built for slot-machine reels and win celebrations. Every asset keeps
a real alpha channel so it composites cleanly over reels/backgrounds.

## Assets (gold)
| Clip  | Preview (plays anywhere) | Transparent video        | Sprite sheet               | Atlas JSON        |
|-------|--------------------------|--------------------------|----------------------------|-------------------|
| Spin  | `coin_spin_preview.mp4`  | `coin_spin.webm` (VP9 α)  | `coin_spin_sheet.png` 10×9 | `coin_spin.json`  |
| Burst | `coin_burst_preview.mp4` | `coin_burst.webm` (VP9 α) | `coin_burst_sheet.png` 9×8 | `coin_burst.json` |

Also `coin_spin.mov` / `coin_burst.mov` — **ProRes 4444** with alpha, for video
editors (After Effects / Premiere / Nuke).

### Which file for what
- **Just to view it** → the `*_preview.mp4` (H.264 over a dark background; opens in any player/browser).
- **HTML5 / Phaser / PixiJS slot** → `*.webm` (VP9 + alpha, transparent) or the sprite sheets.
- **Unity / engine sprite anim** → the sprite sheets + JSON.
- **Video / motion-graphics pipeline** → the ProRes `*.mov` (alpha).

> ProRes `.mov` is a pro editing codec — most ordinary players show a blank frame
> because they can't decode it. Use the MP4 to preview, the ProRes to edit.

## Using the sprite sheets

**Phaser 3**
```js
this.load.atlas('coin', 'coin_spin_sheet.png', 'coin_spin.json');
// frames are named spin_00 … spin_89
this.anims.create({
  key: 'coin-spin',
  frames: this.anims.generateFrameNames('coin', { prefix: 'spin_', start: 0, end: 89, zeroPad: 2 }),
  frameRate: 30, repeat: -1,
});
```

**Unity** — import `coin_spin_sheet.png`, Sprite Mode = Multiple, slice Grid By Cell
Size 160×160, then drag the 90 sprites onto an Animation clip at 30 fps (loop on for
spin, loop off for burst).

## Live preview / editing
- `coin.html` — the animation on an HTML `<canvas>`. Toggle **Spin / Win-burst**,
  pick a **metal** (gold/silver/bronze), and **Record .webm** (transparent WebM,
  Chromium). Checkerboard is page-only; the canvas stays transparent.
- `render.mjs` — headless renderer that produces every asset above.

## Re-render
```bash
ln -sf "$(npm root -g)/playwright" node_modules/playwright   # if needed
node render.mjs
```
Requires a full `ffmpeg` (with `prores_ks`) on PATH.

### Silver / bronze
The metal system is fully built in; only gold is output by default. To also emit
silver and bronze, set `const METALS = ["gold","silver","bronze"];` at the top of
`render.mjs` and re-run — you'll get `coin_spin_silver.*`, `coin_burst_bronze.*`, etc.

## Why the .mov is rendered here (not in the browser)
Browsers can't export a transparent `.mov` — `MediaRecorder` only does WebM/MP4.
So the page records transparent **WebM**, and the true transparent **ProRes 4444
`.mov`** (standard for After Effects / Premiere / Nuke / Unity / Unreal) is produced
by `render.mjs`.
