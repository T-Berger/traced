# Swarm Detector: 3-minute talk

- `visuals/`: four small animated visuals, one per slide. Each is a self-contained HTML file (open it in a browser, it loops) with a ready-made `.mp4` and `.gif` for PowerPoint (Insert > Video).
  1. `1_flip`: bot or human, then flip the labels
  2. `2_attack`: a swarm acts on a post: coordinated, directed, intentional
  3. `3_timing_alone`: timing alone flags everyone
  4. `4_timing_meaning`: timing + embedding space isolates the swarm
- `deck.md` / `deck.pdf`: the Marp slide version (InnoButler theme, not in this repo).

The two example comments and the 40 dots are illustrative, not data. The only real number is 8.4M time-close pairs (`swarm-detector/analysis/results/04_webs.html`, mode "anywhere", 300 s window).

Re-export a visual (needs `ffmpeg` and `puppeteer-core`; the paths at the top of `visuals/export.mjs` point at a local install):

```sh
cd swarm-detector/presentation/visuals && node export.mjs 1_flip video
```
