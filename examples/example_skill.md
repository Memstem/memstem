---
id: 01940000-0000-7000-0000-000000000001
type: skill
title: Send a chat notification to the on-call channel
created: 2026-04-25T16:00:00-04:00
updated: 2026-04-25T16:00:00-04:00
source: human
scope: universal
tags: [chat, notifications, on-call]
prerequisites:
  - "[[Chat webhook URL in env file]]"
verification: Run `bash ~/scripts/notify-send.sh "test"` and check the message appears in the on-call channel
---

# Send a chat notification to the on-call channel

Use the convenience helper:

```bash
bash ~/scripts/notify-send.sh "Your message here"
```

From a file or stdin:

```bash
bash ~/scripts/notify-send.sh --file /tmp/report.txt
echo "Build complete" | bash ~/scripts/notify-send.sh --stdin --title "Deploy done"
```

## Prerequisites

- Webhook URL in `~/.config/notify/.env`

## Pitfalls

- Long messages auto-chunk; don't worry about length
- Only send when someone explicitly asked to be notified
