# WILD coin — Temple of Gold (spin & win-burst)

A glossy gold **WILD** Aztec coin for a digital slot machine, on a **transparent
background**. WILD text on both faces, a sun-face medallion, a solid thick
horizontally-reeded edge, and a full 360° sideways spin. Every asset keeps a real
alpha channel so it composites cleanly over reels/backgrounds.

## Assets (gold)
| Clip  | Preview (plays anywhere) | Transparent video        | Sprite sheet               | Atlas JSON        |
|-------|--------------------------|--------------------------|----------------------------|-------------------|
| Spin  | `coin_spin_preview.mp4`  | `coin_spin.webm` (VP9 α)  | `coin_spin_sheet.png` 10×9 | `coin_spin.json`  |
| Burst | `coin_burst_preview.mp4` | `coin_burst.webm` (VP9 α) | `coin_burst_sheet.png` 9×8 | `coin_burst.json` |

Also `coin_spin.mov` / `coin_burst.mov` — **ProRes 4444** with alpha, for video
editors (After Effects / Premiere / Nuke).

- **Spin** = 90-frame seamless 360° loop (tiles cleanly on a reel).
- **Win-burst** = 72-frame one-shot: the coin pops in with a flash ring and a
  shower of smaller WILD coins.
- Sprite cells are **160×160**, left→right, top→bottom. Atlas JSON is Phaser
  JSONHash; `meta.grid` + `meta.animation` make Unity slicing trivial.

## Which file for what
- **Just to view it** → the `*_preview.mp4` (plays in any player/browser).
- **HTML5 / Phaser / PixiJS slot** → the `*.webm` (VP9 + alpha) or the sprite sheets.
- **Unity / engine sprite animation** → the sprite sheets + JSON.
- **Video / motion-graphics pipeline** → the ProRes `*.mov` (alpha).

## Live page & re-render
- `coin.html` / `index.html` — the animation on a `<canvas>`. Spin / Win-burst
  toggle, metal finish (gold/silver/bronze), and a transparent-WebM recorder.
- `render.mjs` — headless renderer that produces every asset above.
  ```bash
  ln -sf "$(npm root -g)/playwright" node_modules/playwright   # if needed
  node render.mjs
  ```
  Requires a full `ffmpeg` (with `prores_ks`) on PATH. Set `METALS` in
  `render.mjs` to also emit silver/bronze.

> A browser can't export a transparent `.mov` (MediaRecorder is WebM/MP4 only),
> so the page records transparent **WebM** and the ProRes `.mov` comes from `render.mjs`.
