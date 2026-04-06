# uptime-probe-tool

Lightweight uptime checker that pings your services and alerts you when they go down.

## Why

I got tired of finding out my side projects were down from angry users. This thing just sits in a terminal (or a systemd service, or a container) and keeps an eye on your URLs. When something stops responding, it lets you know.

## Quick start

Create a `services.json` in the project root:

```json
[
  {
    "name": "google",
    "url": "https://www.google.com",
    "expected_status": 200
  },
  {
    "name": "my-api",
    "url": "https://api.example.com/health",
    "expected_status": 200,
    "method": "GET",
    "headers": {
      "Authorization": "Bearer your-token-here"
    }
  },
  {
    "name": "status-page",
    "url": "https://status.example.com",
    "expected_status": 200
  }
]
```

Then run it:

```bash
pip install -r requirements.txt
python main.py --config services.json --interval 60 --verbose
```

That's it. It'll check every 60 seconds and print results to the terminal.

## One-off check

Don't need the loop? Just want to verify everything right now:

```bash
python main.py --config services.json --check-once
```

Exits 0 if all healthy, 1 if anything is down. Handy for CI or cron.

## Alerts

By default it just logs to stdout. You can set up email or webhook alerts.

### Webhook (Slack, Discord, etc.)

```bash
python main.py --config services.json --alert-type webhook --webhook-url "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
```

The payload is JSON with `service`, `url`, `error`, `time`, and `status` fields.

### Email

```bash
python main.py \
  --config services.json \
  --alert-type email \
  --smtp-host smtp.gmail.com \
  --smtp-port 587 \
  --smtp-user you@gmail.com \
  --smtp-password your-app-password \
  --smtp-from you@gmail.com \
  --smtp-to oncall@example.com
```

You can also do `--alert-type both` to fire both email and webhook.

### Alert cooldown

To avoid getting spammed, there's a cooldown between alerts for the same service. Default is 15 minutes. Change it with `--cooldown-minutes 30`.

## Config format

Each service in the JSON array supports:

| Field | Required | Default | Notes |
|---|---|---|---|
| `name` | no | URL | Friendly name for logs/alerts |
| `url` | yes | – | Full URL to probe |
| `expected_status` | no | 200 | What status code counts as healthy |
| `method` | no | GET | HTTP method |
| `headers` | no | {} | Extra headers (auth, custom user-agent, etc.) |

## State persistence

The tool saves probe state to `.uptime_state.json` so it remembers consecutive failure counts and alert cooldowns across restarts. Delete that file if you want a clean slate.

## Running as a service

Drop it in a systemd unit and forget about it:

```ini
[Unit]
Description=Uptime Probe Tool
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/uptime-probe-tool
ExecStart=/usr/bin/python3 /opt/uptime-probe-tool/main.py --config services.json --interval 60
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## CLI options

```
-c, --config              Path to services JSON (default: services.json)
-i, --interval            Seconds between checks (default: 60)
-t, --timeout             Request timeout in seconds (default: 10)
-r, --retries             Retries per check before marking down (default: 3)
--check-once              Single check then exit
--verbose                 Show OK results in loop mode
--alert-type              log | email | webhook | both
--cooldown-minutes        Minutes between repeated alerts (default: 15)
--smtp-host               SMTP server
--smtp-port               SMTP port
--smtp-user               SMTP username
--smtp-password           SMTP password
--smtp-from               Sender email
--smtp-to                 Recipient email
--smtp-no-tls             Disable SMTP TLS
--webhook-url             Webhook URL for alerts
```

## Notes

- Uses stdlib `urllib` – no external HTTP dependencies
- `requirements.txt` is empty but kept for convention and future deps
- Graceful shutdown on Ctrl+C, state is saved before exit
- Retries with 1s delay between attempts before marking a service as down
