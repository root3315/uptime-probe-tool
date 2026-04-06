#!/usr/bin/env python3
"""uptime-probe-tool – lightweight uptime checker that pings your services
and alerts you when they go down."""

import argparse
import json
import os
import signal
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_CONFIG_PATH = "services.json"
DEFAULT_INTERVAL = 60
DEFAULT_TIMEOUT = 10
DEFAULT_RETRIES = 3
STATE_FILE = ".uptime_state.json"

running = True


def handle_signal(signum, frame):
    global running
    running = False
    print("\n[info] received shutdown signal, wrapping up...")


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def load_config(config_path):
    path = Path(config_path)
    if not path.exists():
        print(f"[error] config file not found: {config_path}")
        print(f"[info] create a JSON file with a list of services, e.g.:")
        print(json.dumps([
            {"name": "google", "url": "https://www.google.com", "expected_status": 200},
            {"name": "my-api", "url": "https://api.example.com/health", "expected_status": 200}
        ], indent=2))
        sys.exit(1)

    with open(path, "r") as f:
        services = json.load(f)

    if not isinstance(services, list):
        print("[error] config must be a JSON array of service objects")
        sys.exit(1)

    validated = []
    for svc in services:
        if "url" not in svc:
            print(f"[warn] skipping service without url: {svc.get('name', 'unknown')}")
            continue
        validated.append({
            "name": svc.get("name", svc["url"]),
            "url": svc["url"],
            "expected_status": svc.get("expected_status", 200),
            "method": svc.get("method", "GET"),
            "headers": svc.get("headers", {}),
        })

    if not validated:
        print("[error] no valid services found in config")
        sys.exit(1)

    return validated


def load_state():
    path = Path(STATE_FILE)
    if path.exists():
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def probe_service(service, timeout):
    url = service["url"]
    method = service["method"]
    headers = service.get("headers", {})

    req = Request(url, method=method, headers=headers)
    start = time.monotonic()

    try:
        resp = urlopen(req, timeout=timeout)
        elapsed = time.monotonic() - start
        status_code = resp.getcode()
        body = resp.read()
        return {
            "success": True,
            "status_code": status_code,
            "response_time": round(elapsed, 3),
            "response_size": len(body),
        }
    except HTTPError as e:
        elapsed = time.monotonic() - start
        return {
            "success": False,
            "status_code": e.code,
            "response_time": round(elapsed, 3),
            "error": str(e),
        }
    except (URLError, TimeoutError, OSError) as e:
        elapsed = time.monotonic() - start
        return {
            "success": False,
            "status_code": None,
            "response_time": round(elapsed, 3),
            "error": str(e),
        }


def send_email_alert(service_name, url, error, smtp_config):
    subject = f"[DOWN] {service_name} is not responding"
    body_lines = [
        f"Service: {service_name}",
        f"URL: {url}",
        f"Error: {error}",
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "This is an automated alert from uptime-probe-tool.",
    ]
    msg = MIMEText("\n".join(body_lines))
    msg["Subject"] = subject
    msg["From"] = smtp_config["from"]
    msg["To"] = smtp_config["to"]

    try:
        if smtp_config.get("use_tls", True):
            server = smtplib.SMTP_SSL(smtp_config["host"], smtp_config.get("port", 465))
        else:
            server = smtplib(smtp_config["host"], smtp_config.get("port", 587))
            server.starttls()

        server.login(smtp_config["user"], smtp_config["password"])
        server.send_message(msg)
        server.quit()
        print(f"[alert] email sent for {service_name}")
    except Exception as e:
        print(f"[error] failed to send email alert: {e}")


def send_webhook_alert(service_name, url, error, webhook_url):
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "service": service_name,
        "url": url,
        "error": error,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "status": "down",
    }).encode()

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"[alert] webhook sent for {service_name} (status {resp.getcode()})")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"[error] failed to send webhook alert: {e}")


def format_status(service, result, consecutive_fails):
    name = service["name"]
    url = service["url"]
    expected = service["expected_status"]

    if result["success"]:
        status_icon = "OK"
        detail = f"{result['status_code']} | {result['response_time']:.3f}s | {result['response_size']}B"
        if consecutive_fails > 0:
            detail += f" (recovered after {consecutive_fails} failures)"
    else:
        status_icon = "DOWN"
        detail = f"expected {expected}, got {result['status_code']}" if result["status_code"] else result["error"]

    timestamp = datetime.now().strftime("%H:%M:%S")
    return f"[{timestamp}] [{status_icon:>4}] {name:<20} {url:<50} {detail}"


def run_probe_loop(services, interval, timeout, retries, alert_config, verbose):
    state = load_state()
    consecutive_fails = {svc["name"]: state.get(svc["name"], {}).get("consecutive_fails", 0) for svc in services}
    alert_cooldown = {svc["name"]: state.get(svc["name"], {}).get("alert_cooldown_until", 0) for svc in services}

    print(f"[info] starting uptime probe – monitoring {len(services)} service(s) every {interval}s")
    print(f"[info] press Ctrl+C to stop\n")

    while running:
        for service in services:
            name = service["name"]
            url = service["url"]
            expected = service["expected_status"]

            result = None
            for attempt in range(1, retries + 1):
                result = probe_service(service, timeout)
                if result["success"] and result["status_code"] == expected:
                    break
                if attempt < retries:
                    time.sleep(1)

            is_healthy = result["success"] and result["status_code"] == expected

            if is_healthy:
                if consecutive_fails[name] > 0:
                    print(format_status(service, result, consecutive_fails[name]))
                elif verbose:
                    print(format_status(service, result, 0))
                consecutive_fails[name] = 0
                alert_cooldown[name] = 0
            else:
                consecutive_fails[name] += 1
                print(format_status(service, result, consecutive_fails[name]))

                now = time.time()
                cooldown_until = alert_cooldown.get(name, 0)
                cooldown_minutes = alert_config.get("cooldown_minutes", 15)

                if now > cooldown_until:
                    alert_type = alert_config.get("type", "log")

                    if alert_type == "email":
                        send_email_alert(name, url, result.get("error", "unknown"), alert_config["smtp"])
                    elif alert_type == "webhook":
                        send_webhook_alert(name, url, result.get("error", "unknown"), alert_config["webhook_url"])
                    elif alert_type == "both":
                        send_email_alert(name, url, result.get("error", "unknown"), alert_config["smtp"])
                        send_webhook_alert(name, url, result.get("error", "unknown"), alert_config["webhook_url"])

                    alert_cooldown[name] = now + (cooldown_minutes * 60)

        state = {}
        for svc in services:
            state[svc["name"]] = {
                "consecutive_fails": consecutive_fails[svc["name"]],
                "alert_cooldown_until": alert_cooldown[svc["name"]],
            }
        save_state(state)

        if not running:
            break

        for _ in range(interval):
            if not running:
                break
            time.sleep(1)

    print("\n[info] probe loop stopped, saving state...")
    save_state(state)


def run_single_check(services, timeout, retries):
    print(f"[info] running single check against {len(services)} service(s)\n")

    all_healthy = True
    for service in services:
        result = None
        for attempt in range(1, retries + 1):
            result = probe_service(service, timeout)
            if result["success"] and result["status_code"] == service["expected_status"]:
                break
            if attempt < retries:
                time.sleep(1)

        is_healthy = result["success"] and result["status_code"] == service["expected_status"]
        if not is_healthy:
            all_healthy = False
        print(format_status(service, result, 0))

    print()
    if all_healthy:
        print("[info] all services are healthy")
        sys.exit(0)
    else:
        print("[warn] one or more services are down")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="uptime-probe-tool – lightweight uptime checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s --config services.json --interval 30
  %(prog)s --check-once --config services.json
  %(prog)s --config services.json --alert-type webhook --webhook-url https://hooks.slack.com/...
        """,
    )

    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH, help=f"path to services config (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("-i", "--interval", type=int, default=DEFAULT_INTERVAL, help=f"check interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("-t", "--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"request timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("-r", "--retries", type=int, default=DEFAULT_RETRIES, help=f"retry count per check (default: {DEFAULT_RETRIES})")
    parser.add_argument("--check-once", action="store_true", help="run a single check and exit")
    parser.add_argument("--verbose", action="store_true", help="show OK results in loop mode")
    parser.add_argument("--alert-type", choices=["log", "email", "webhook", "both"], default="log", help="alert method (default: log)")
    parser.add_argument("--cooldown-minutes", type=int, default=15, help="minutes between repeated alerts (default: 15)")
    parser.add_argument("--smtp-host", help="SMTP server host for email alerts")
    parser.add_argument("--smtp-port", type=int, help="SMTP server port")
    parser.add_argument("--smtp-user", help="SMTP username")
    parser.add_argument("--smtp-password", help="SMTP password")
    parser.add_argument("--smtp-from", help="email sender address")
    parser.add_argument("--smtp-to", help="email recipient address")
    parser.add_argument("--smtp-no-tls", action="store_true", help="disable TLS for SMTP")
    parser.add_argument("--webhook-url", help="webhook URL for alerts")

    args = parser.parse_args()

    services = load_config(args.config)

    alert_config = {"type": args.alert_type, "cooldown_minutes": args.cooldown_minutes}

    if args.alert_type in ("email", "both"):
        if not all([args.smtp_host, args.smtp_user, args.smtp_password, args.smtp_from, args.smtp_to]):
            print("[error] email alerts require --smtp-host, --smtp-user, --smtp-password, --smtp-from, --smtp-to")
            sys.exit(1)
        alert_config["smtp"] = {
            "host": args.smtp_host,
            "port": args.smtp_port or (587 if not args.smtp_no_tls else 465),
            "user": args.smtp_user,
            "password": args.smtp_password,
            "from": args.smtp_from,
            "to": args.smtp_to,
            "use_tls": not args.smtp_no_tls,
        }

    if args.alert_type in ("webhook", "both"):
        if not args.webhook_url:
            print("[error] webhook alerts require --webhook-url")
            sys.exit(1)
        alert_config["webhook_url"] = args.webhook_url

    if args.check_once:
        run_single_check(services, args.timeout, args.retries)
    else:
        run_probe_loop(services, args.interval, args.timeout, args.retries, alert_config, args.verbose)


if __name__ == "__main__":
    main()
