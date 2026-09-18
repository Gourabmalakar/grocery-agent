---
name: health-monitor
description: Detect and report breakage anywhere in the pipeline — a dead adapter, a stuck job, an unreachable console, a broken hash chain — and alert with what broke, what it means, and what's needed. Use on every scheduled check and whenever something unexpected fails.
---

# Health Monitor

## The alert contract

Every alert states three things, following the family office's `send_alert`
pattern: **what broke**, **what it means** for the person reading it, and
**what's needed** from them. A severity with no clear next action is not
useful, however accurate.

## What "healthy" means here

`GET /health` on the Console reports: mode, kill-switch tier, both chains'
verification status, spend today/month against caps, any order stuck in a
pending state, and the sentinel's own freshness (`sentinel.last_swept_at`,
`sentinel.dead_mans_switch_tripped`). A response that omits one of these
because it couldn't be computed is itself the alert — a degraded check must
say so, never go quiet.

Currently wired as far as: `grocery/sentinel/checks.py` + `report.py` do the
actual detection and can be run on demand (Control page's "Run a check now",
or `python -m grocery.sentinel.report`) — a fatal finding trips PANIC
immediately, same as this skill describes. Not yet wired: a scheduled
cadence (the plan's heartbeat) and the alert email/dedup layer below — until
those land, "detect and report" means the Control page and `/health`, not an
unattended notification.

## Rules

- Deduplicate: the same broken thing alerting every cycle trains the human
  to stop reading alerts, and then the genuinely new one gets missed too.
  Key on a hash of (severity, what) and suppress repeats within a day.
- A dead-man's switch matters as much as a positive alert: if the health
  check itself hasn't run in the expected window, that absence is worth
  reporting — silence that looks like a quiet day is the failure this
  system is least allowed to have.
