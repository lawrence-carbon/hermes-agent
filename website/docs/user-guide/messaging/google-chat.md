---
title: "Google Chat Setup"
description: "Connect Hermes Agent to Google Chat spaces using a service account"
---

# Google Chat Setup

Hermes supports Google Chat through the gateway using a Google Cloud service account and the Google Chat REST API.
Unlike Slack/WhatsApp webhook setups, Hermes reads messages by polling configured spaces via API.

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

Open **Google Cloud Console → Google Chat API → Configuration** and fill these sections:

### A. App details

- Set **App name**, **Avatar URL**, and **Description**.
- Save.

### B. Interactive features (important)

Turn on **Interactive features** and set:

- **Receive 1:1 messages**: ON
- **Join spaces and group conversations**: ON (recommended if you want Hermes in spaces)

If you only want direct messages, you can leave group conversations off.

If your Google Cloud Console is in French, these labels map to:

| French UI label | English UI label | Recommended value |
|-----------------|------------------|-------------------|
| `Activer les fonctionnalites interactives` | Enable interactive features | ON |
| `Rejoindre des espaces et des conversations de groupe` | Join spaces and group conversations | ON (for spaces) |
| `URL du point de terminaison HTTP` | HTTP endpoint URL | Selected |
| `Utiliser une URL ... commune pour tous les declencheurs` | Use a common HTTP endpoint URL for all triggers | Selected (simplest) |

### C. Connection settings and triggers

In the same configuration page:

1. Under **Connection settings**, select **HTTP endpoint URL**.
2. Under **Triggers**, easiest option is:
   - **Use a common HTTP endpoint URL for all triggers**
3. Enter a valid HTTPS URL that you control.

If your console is set to "separate URL per trigger", fill all three with the same URL:

- **App command**
- **Added to space**
- **Message**

:::note Why this is still required
Google Chat asks for trigger URLs when interactive features are enabled. Hermes does not consume these push events directly; it polls spaces using the Chat API with your service account.  
Use any stable HTTPS endpoint that returns `200 OK` (for example, a tiny Cloud Run "hello" endpoint).
:::

### D. App identity and availability

- Ensure the app is configured to run as your chosen app identity/service account in this project.
- Set visibility/availability so your test users can find and add the app.
- Publish the app internally in your Workspace as needed by your admin policy.

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

## Troubleshooting the Configuration screen

| Problem | What to check |
|---------|---------------|
| Configuration page requires trigger URLs | Keep **Interactive features** ON, choose **common HTTP endpoint**, and provide one valid HTTPS URL. |
| Bot can receive DMs but cannot be added to spaces | Enable **Join spaces and group conversations** in Interactive features. |
| Hermes starts but receives no Google Chat messages | Confirm `GOOGLECHAT_SPACES` contains exact `spaces/...` IDs and the app is added to those spaces. |
| API auth errors on startup | Verify `GOOGLECHAT_SERVICE_ACCOUNT` points to valid JSON with `client_email` and `private_key`. |

## Security notes

- Default policy is deny-by-default if no allowlist/pairing applies.
- Prefer `GOOGLECHAT_ALLOWED_USERS` and/or global `GATEWAY_ALLOWED_USERS`.
- Keep the service-account JSON secret; rotate keys if exposed.
