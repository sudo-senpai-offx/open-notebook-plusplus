# Pending

## Governor worker sleep/wake
Checkpoint: Implementation
Waiting for: confirmation
Since: 2026-10-11
Proposal: supervisord.conf add unix_http_server (additive); governor action layer with injectable supervisor runner: sleep path (lease clear + would_hibernate -> set draining + stop worker), POST /api/governor/wake (clear draining + start worker); worker/API/UI/DB untouched otherwise; media + full stop order deferred; tests mock runner (sleep fires, veto blocks, wake restarts); full battery + local commit.
Scope: supervisord.conf, api/governor.py, api/routers/ (wake endpoint home TBD during implementation), tests
