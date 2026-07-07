# Channel integration & validation

ONASSIS publishes products to **sales channels** (Etsy, Shopify) and distributes
marketing to **reach channels** (Pinterest, Facebook, Instagram, Email, Blog).
Every channel is a safe no-op until you add its credentials, so you connect and
validate them one at a time from the Operations Centre — no code changes.

The product lifecycle is separated from the publishing channel, so adding a
channel is a connector, not a rewrite (Amazon / eBay / TikTok Shop could slot in
the same way).

## 0. The Integrations page

**System → Integrations** is the single place to configure, test, monitor and
diagnose every external service — AI providers, marketplaces, marketing channels
and production. Each connector has a card with a live health indicator
(🟢 Healthy · 🟡 Warning · 🔴 Failed · ⚪ Not configured), editable + masked
credentials, a real **Test Connection**, guided setup steps, and an activity log.
You can onboard a connector entirely from the browser — the values below are the
underlying env vars, but you no longer have to edit `.env` by hand.

## 1. Credentials (env vars, or paste them on the Integrations page)

| Channel | Variables |
|---|---|
| Shopify | `SHOPIFY_STORE_DOMAIN` (`your-store.myshopify.com`), `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` (from a Dev Dashboard app — ONASSIS obtains the access token itself via the client-credentials grant), optional `SHOPIFY_LOCATION_ID`, `shopify.blog_id` (config, for Blog). A legacy `SHOPIFY_ADMIN_TOKEN` is still honoured if you already have one. |
| Facebook | `META_PAGE_ACCESS_TOKEN`, `FACEBOOK_PAGE_ID` |
| Instagram | `META_PAGE_ACCESS_TOKEN`, `INSTAGRAM_USER_ID` (an IG **Business** account) |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` |
| Pinterest | `PINTEREST_ACCESS_TOKEN`, `PINTEREST_BOARD_ID` |

Restart (Operations Centre → Software Updates → the systemd restart bridge).

## 2. Validate from the Operations Centre (no SSH)

**Marketing tab → Channel connections → Test Connections.** Read-only checks:

- **Shopify** — `GET /shop.json` confirms the store + token (shows the shop name).
- **Facebook / Instagram** — confirms the page/IG-user node resolves with the token.
- **Email** — connects + authenticates to SMTP without sending.

Then:

- **Send Test Email** — delivers a validation email to `EMAIL_TO`. Check
  formatting, links and images.
- **Distribute Now** — ships any pending Instagram / Facebook / Blog / Email
  assets immediately (instead of waiting for the daily cycle).

## 3. Validate publishing

Run the daily cycle (**RUN BUSINESS**) or publish a campaign, then open the
product in the **Products** tab. The **Sales Channels** matrix shows exactly
where the product exists:

| Channel | Example status |
|---|---|
| Etsy | Draft Created / Live / Failed |
| Shopify | Published / Draft Created / Failed |
| Pinterest | Posted (n) / Scheduled (n) |
| Facebook / Instagram | Posted / Skipped / Failed |
| Email | Sent / Skipped |
| Blog | Published / Skipped |

Verify on Shopify: the product's **images**, **price**, **variants**, **SEO**
(title/description/tags), and a published **blog article** if configured.

## 4. Prove the operator controls

**System → Business Settings** toggles are real controls:

- **Facebook OFF** → Facebook assets are recorded *Skipped* (nothing posted).
- **Facebook ON** → the next post is created.
- **Marketing OFF** → all marketing distribution is skipped.

> Instagram needs a publicly reachable image URL (a Graph API requirement). Until
> a public asset URL is available it is recorded *Skipped*, never *Failed*.

Once every channel reads **connected** and a real product shows the right status
on each, the channels are production-validated — then move on to the learning &
analytics feedback loops.
