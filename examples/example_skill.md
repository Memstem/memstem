---
id: 01940000-0000-7000-0000-000000000001
type: skill
title: Send Telegram message to Brad
created: 2026-04-25T16:00:00-04:00
updated: 2026-04-25T16:00:00-04:00
source: human
scope: universal
tags: [telegram, notifications, brad]
prerequisites:
  - "[[Telegram bot token in env file]]"
verification: Run `bash ~/scripts/tg-send "test"` and check Brad's Telegram receives it
---

# Send Telegram to Brad

Use the convenience helper:

```bash
bash ~/scripts/tg-send "Your message here"
```

From a file or stdin:

```bash
bash ~/scripts/tg-send --file /tmp/report.txt
echo "Build complete" | bash ~/scripts/tg-send --stdin --title "Deploy done"
```

## Prerequisites

- Bot token in `~/claude-telegram-relay/.env`

## Pitfalls

- Long messages auto-chunk; don't worry about length
- Only send when explicitly requested by Brad
