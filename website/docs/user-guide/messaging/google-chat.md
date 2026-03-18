---
title: "Google Chat Setup"
description: "Connect Hermes Agent to Google Chat spaces using a service account"
---

# Google Chat Setup

Hermes supports Google Chat through the gateway using a Google Cloud service account and the Google Chat REST API.

## Requirements

- A Google Cloud project
- Google Chat API enabled
- A Google Chat app configured in that project
- A service account key (JSON)
- The Chat app added to one or more spaces

## 1) Enable Google Chat API

1. Open Google Cloud Console for your project.
2. Go to **APIs & Services → Library**.
3. Enable **Google Chat API**.

## 2) Configure a Google Chat app

1. In Google Cloud Console, open **Google Chat API → Configuration**.
2. Configure app details (name, avatar, description).
3. Set interaction behavior appropriate for your workspace.
4. Ensure your app identity uses a service account.

## 3) Create service account key

1. Go to **IAM & Admin → Service Accounts**.
2. Create/select the service account used by your Chat app.
3. Create a **JSON** key.
4. Store it securely (recommended path: `~/.hermes/googlechat-service-account.json`).

## 4) Add Hermes app to spaces

Add the configured Chat app to each space where Hermes should receive/respond to messages.

You will need the resource names for polling and delivery:

- Space format: `spaces/AAAAxxxx`

## 5) Configure environment variables

In `~/.hermes/.env`:

```bash
# JSON blob or file path to service account credentials
GOOGLECHAT_SERVICE_ACCOUNT=/absolute/path/to/service-account.json

# Spaces Hermes should poll for inbound messages
GOOGLECHAT_SPACES=spaces/AAAAxxxx,spaces/BBBByyyy

# Optional allowlist (recommended)
GOOGLECHAT_ALLOWED_USERS=users/123456789,users/987654321

# Optional default delivery target for cron/send_message
GOOGLECHAT_HOME_CHANNEL=spaces/AAAAxxxx
```

Optional tuning:

```bash
GOOGLECHAT_POLL_INTERVAL=5
GOOGLECHAT_HTTP_TIMEOUT=20
```

## 6) Start gateway

```bash
hermes gateway run
```

Or install/start as a service:

```bash
hermes gateway install
hermes gateway start
```

## 7) Verify

- Send a message in one of the configured spaces.
- Hermes should respond in-thread when a thread exists.
- For proactive sends (cron/send_message), test target `googlechat` (home channel) or `googlechat:spaces/AAAAxxxx`.

## Security notes

- Default policy is deny-by-default if no allowlist/pairing applies.
- Prefer `GOOGLECHAT_ALLOWED_USERS` and/or global `GATEWAY_ALLOWED_USERS`.
- Keep the service-account JSON secret; rotate keys if exposed.
