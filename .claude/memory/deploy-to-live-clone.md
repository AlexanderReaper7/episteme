---
name: deploy-to-live-clone
description: The user pre-approved deploying Episteme to the live clone in ~/Projects/selfhosting/episteme; deploy without asking.
metadata:
  type: feedback
---

Deploy finished work to the live stack in `~/Projects/selfhosting/episteme` without asking first (user, 2026-10-10: "yes, always deploy").

**Why:** asking before every deploy left verified fixes undeployed while the user was away, e.g. the Matsedel double-header fix sat on the dev server only.

**How to apply:** fast-forward the clone from this checkout (`git -C ~/Projects/selfhosting/episteme pull --ff-only ~/Projects/episteme main`). `web` reloads `src` by itself; anything under the worker's packages needs `docker compose build worker && docker compose up -d worker` there. Pushing to GitHub is a separate approval and still needs asking. Never `docker compose down -v`.
