# Codex for OSS — Application Submission Sheet

Repo: https://github.com/esp8266good/pig-agri
Form: https://openai.com/form/codex-for-oss/ (requires ChatGPT login)

> Each answer is capped at 500 characters. Verified lengths: Q1=490, Q2=467, Q3=419.

## Q1 — Why does this repository qualify? (490)

pig-agri is a real-time computer-vision system deployed on a working pig farm: it tracks each pig's activity to flag animals needing veterinary blood tests, improving welfare and cutting unneeded draws. It's non-commercial research (my thesis basis). As sole maintainer I keep 240+ passing tests, practice spec-driven development, and document systematic debugging across 100+ commits. The repo is new so stars are low, but this is actively maintained, deployed production code, not a demo.

## Q2 — How will you use API credits for your project? (467)

As a solo maintainer, Codex would let one person responsibly sustain a full-stack real-time CV system: expanding the test suite and CI, refactoring for clarity, and reviewing my own diffs for code quality and security. It would accelerate features (multi-camera, stronger ReID, automated blood-draw reports) and help me systematically verify and fix the many long-running stability issues in my backlog, freeing scarce time for the underlying animal-welfare research.

## Q3 — Anything else we should know? (419)

I'm one researcher, not a funded team, building this for animal welfare in agriculture, an underserved domain for AI tooling. The repo is young but the system is real and running on an actual farm. Codex wouldn't polish a popular library; it would let a single person maintain and document production code while completing the research it supports. Happy to share deployment details, demo footage, or a maintainer call.

---

## GitHub repo settings (set manually on github.com → repo → About / ⚙)

**Description:**

> Real-time computer-vision system for pig farms: tracks each pig's activity to flag animals needing veterinary blood tests. Deployed, non-commercial research.

**Topics:**

`computer-vision` `object-tracking` `multi-object-tracking` `animal-welfare`
`precision-livestock-farming` `fastapi` `yolox` `agriculture` `hls`

---

## Pre-submit checklist

- [ ] LICENSE, README, CONTRIBUTING, CODE_OF_CONDUCT committed and visible on GitHub
- [ ] CI badge is green on the repo home page
- [ ] GitHub description + topics set (above)
- [ ] Signed in to the ChatGPT account used for the application
- [ ] Paste Q1–Q3, submit

## Honest expectations

The program weighs ecosystem importance and repository usage (stars/downloads), where this
repo is currently weak (new, 0 stars). This submission maximizes the controllable signals —
real deployment, disciplined maintenance, clear OSS hygiene — but approval is not guaranteed.
