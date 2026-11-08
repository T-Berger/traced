---
marp: true
theme: innobutler
paginate: true
title: Swarm Detector
description: Three-minute talk for the boss. Bots now sound human one by one, so we look for the group instead.
---

<div class="title">
<p class="eyebrow">The problem · Which one is the bot?</p>
<h2>One by one, a bot and a human now read the same</h2>
<p class="sub">Two comments under the same war-news post. Pick the bot.</p>
</div>

<div class="cards">
<div class="card"><h3>Comment A · 🤖 Bot</h3><p>"Honestly can't believe they left the town undefended again. Third night this week and nobody answers for it."</p></div>
<div class="card"><h3>Comment B · 🙂 Human</h3><p>"Same here. Three nights this week and still nobody is accountable. I'm tired of it."</p></div>
</div>

<p class="note">Illustrative pair written for this slide, not taken from the data.</p>

<!--
Start here. Ask the room: which one is the bot? Let them guess for a few seconds, then click to the next slide.
Point: since ChatGPT, a bot writes fluent, natural, on-topic text. Nothing in a single comment gives it away.
-->

---

<div class="title">
<p class="eyebrow">The problem · Flip them</p>
<h2>Swap the labels. Nothing in the text changes.</h2>
<p class="sub">So we stop judging single comments. A swarm is different in three ways.</p>
</div>

<div class="cards">
<div class="card"><h3>Comment A · 🙂 Human</h3><p>"Honestly can't believe they left the town undefended again. Third night this week and nobody answers for it."</p></div>
<div class="card"><h3>Comment B · 🤖 Bot</h3><p>"Same here. Three nights this week and still nobody is accountable. I'm tired of it."</p></div>
</div>

<div class="cards">
<div class="card accent"><h3>Coordinated</h3><p>Many accounts act together, within minutes.</p></div>
<div class="card"><h3>Directed</h3><p>Aimed at the same posts and channels.</p></div>
<div class="card"><h3>Intentional</h3><p>Repeated again and again, not by chance.</p></div>
</div>

<!--
Click: the labels flip. Same two texts, opposite labels, and it still looks right. That is the point: individual detection is a losing game.
But an attack is a group effort. It is coordinated, directed and intentional, and that leaves a pattern across accounts that one account never shows. We look for the pattern, not the bot.
Also: we do not claim who runs it. We describe what the group does.
-->

---

<div class="title">
<p class="eyebrow">Our method · Timing + meaning</p>
<h2>Timing alone is not enough. Timing plus meaning exposes the hidden pattern.</h2>
</div>

<div class="flow">
<div class="node grey" style="width:340px"><b>Same time</b><span>8.4M pairs within 5 min</span></div>
<div class="arr">→</div>
<div class="node amber" style="width:340px"><b>Same meaning</b><span>62,499 also say the same thing</span></div>
<div class="arr">→</div>
<div class="node amber" style="width:340px"><b>Again and again</b><span>822 pairs repeat on 2+ days</span></div>
<div class="arr">→</div>
<div class="node green" style="width:340px"><b>Beats chance?</b><span>keep only what luck cannot explain</span></div>
</div>

<div class="stats">
<div class="stat muted"><b>8.4M</b><span>pairs that are close in time</span></div>
<div class="vs">→</div>
<div class="stat"><b>62,499</b><span>also close in meaning</span></div>
<div class="vs">→</div>
<div class="stat"><b>822</b><span>repeat on 2+ days</span></div>
</div>

<div class="legend"><span><i class="grey"></i>Timing signal</span><span><i class="amber"></i>Meaning signal (embedding space)</span><span><i class="green"></i>Check against shifted timelines</span></div>

<p class="note">Source: swarm-detector analysis, 15 Telegram channels, 14–21 Oct 2026, 300 s window. Reply chats are removed first. Validation on a separate test set comes next.</p>

<!--
Walk the diagram left to right.
1. Timing only: two accounts commenting within five minutes happens 8.4 million times in a busy week. Timing alone is not enough; most of it is people reading the same post.
2. Add meaning: we turn each comment into a vector (multilingual e5 embeddings, so Ukrainian and Russian are compared directly) and keep pairs that are close in time AND close in meaning. 62,499 events.
3. Keep only pairs that do it repeatedly, on at least 2 days: 822.
4. Chance test: shift each account's timeline by whole days. Its habits stay, its synchrony with specific partners breaks. If a real pair is not above its own shifted version, it is coincidence.
Close: timing alone drowns in coincidence, timing plus meaning isolates the pairs that move together. Next step is validation on a separate test set.
Left out for readability: the conversation filter (replies are not events), the per-length similarity cut-off, and the Louvain grouping step.
Only 27% of comments carry a reliable handle; the rest are matched by display name, which is a weaker identity. Say this if asked.
-->
