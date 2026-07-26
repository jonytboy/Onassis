# Reel music (royalty-free)

Drop **royalty-free** audio tracks here (`.mp3`, `.m4a`, `.aac`, `.wav`, `.ogg`,
`.flac`) and the Reel Studio will mux one into each rendered clip — chosen
deterministically per clip (a clip keeps the same track across re-renders) and
looped/trimmed to the video length.

- No tracks here → clips get a **silent** AAC audio stream (still valid for
  TikTok/Buffer/Reels, which reject videos with *zero* audio streams).
- Use only music you are licensed to use. ONASSIS ships no copyrighted audio.
- Point somewhere else with the `REEL_MUSIC_DIR` environment variable.

Good sources for CC0 / royalty-free tracks: Pixabay Music, YouTube Audio
Library (filter to "no attribution"), Uppbeat (with their licence).
