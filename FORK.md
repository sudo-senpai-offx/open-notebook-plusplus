# open-notebook-plusplus — Fork Notes

This repository is **open-notebook-plusplus**: a credited, student-focused
fork of [lfnovo/open-notebook](https://github.com/lfnovo/open-notebook)
(MIT licensed — see [LICENSE](LICENSE), author credit retained).

## What this fork is

Upstream OpenNotebook is an open-source, privacy-focused research assistant
(NotebookLM alternative). This fork keeps all of that and adds a low-end /
student-learning layer on top:

- **Low-end survival** — mini-first model routing, tier-gated heavies,
  capability tiers, idle/hibernate governor with lease guard, percent-of-total
  resource budgets, no-AVX inference sidecar (see `docs/7-DEVELOPMENT/wayfinder-student-os/`).
- **Learning OS utensils** — tiny-model training, FSRS review, pomodoro,
  metrics, Anki/Obsidian flows (see `docs/7-DEVELOPMENT/wayfinder-plusplus/`).

## Upstream pin

- Fork base: upstream commit `20b413551fb6918c0e9f4879b8367d3480665944`
  (`docs(install): verified deploy-on-another-PC path…`, 2026-10-08).
- All fork work sits in commits after that base (subjects tagged
  `low-end`, `T1–T8`, `P1–P8`, or `student-OS`).

## Sync policy

- Upstream fixes flow in via **cherry-pick onto the fork base line**, never a
  blind merge: `git fetch upstream`, review, cherry-pick, full battery green.
- Fork features never go upstream without maintainer discussion; if a change
  belongs upstream (e.g. a generic bug fix), propose it there first.
- Rebase policy: fork branches rebase onto newer upstream pins deliberately
  and rarely (documented here when done), so history stays reviewable.
