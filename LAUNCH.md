# STR Optimizer launch guide

Where everything lives:

| Piece | Host | Cost |
|---|---|---|
| Website (optimizemystr.com) | Netlify | Free |
| API, brief worker, daily scheduler, database | Render | About $20/month |
| Code | GitHub (`launch` branch) | Free |
| Domain | GoDaddy | Already paid |
| Payments | Stripe | Per transaction |
| Email | SendGrid | Depends on plan |

Do the parts in order. Nothing touches the live domain until part 6.

---

## Part 1 — Put the code on GitHub (5 minutes)

1. Go to https://github.com/srhoades1761/str-optimizer-backend-fork/branches and click **New branch**. Name it `launch`, source `main`, click **Create new branch**.
2. Go to https://github.com/srhoades1761/str-optimizer-backend-fork/upload/launch
3. In Finder, open `Downloads/str-optimizer-launch/backend`, press **Cmd+A**, and drag everything onto the GitHub page. This includes hidden-looking files like `.python-version`; if Finder hides it, press **Cmd+Shift+.** to show it first.
4. Commit message: `Launch backend: subscriptions + monthly listing brief`. Choose **Commit directly to the launch branch**, then **Commit changes**.

## Part 2 — Accounts you need (15 minutes)

1. **Render:** sign up at https://render.com with **Sign in with GitHub**, and give it access to `str-optimizer-backend-fork`.
2. **Netlify:** sign up at https://netlify.com (email or GitHub).
3. **SendGrid:** an account in your own name, at https://sendgrid.com. The old setup sent from someone else's Gmail.
   - Settings → **Sender Authentication** → **Authenticate Your Domain** → `optimizemystr.com`, DNS host GoDaddy. It shows 3–5 CNAME records. Add each in GoDaddy (DNS → Add record → CNAME) exactly as shown, then click **Verify** in SendGrid.
   - Settings → **API Keys** → **Create API Key** → Restricted, with **Mail Send** on. Copy it (starts `SG.`). You only see it once.
4. **A real support inbox:** briefs set Reply-To to `support@optimizemystr.com`. In GoDaddy, set up email forwarding from that address to your Gmail if it doesn't exist yet.
5. **Stripe secret key:** Stripe Dashboard → Developers → **API keys** → copy the **Secret key** (`sk_live_…`). Use test mode (`sk_test_…`) first if you want to practice.

Optional, can be added later:
- **AirROI** (market section): https://www.airroi.com/api → sign up → API key. Add $10 of credit.
- **OpenAI** (title rewrite and ready-to-paste text): https://platform.openai.com → API keys.

## Part 3 — Start the backend on Render (10 minutes)

1. Render Dashboard → **New** → **Blueprint**.
2. Pick `str-optimizer-backend-fork`, branch `launch`. Render reads `render.yaml` and lists four things: `str-optimizer-db`, `str-optimizer-api`, `str-optimizer-worker`, `str-optimizer-monthly`.
3. It asks for these values. Paste them:

| Setting | Value |
|---|---|
| `STRIPE_SECRET_KEY` | from Part 2 step 5 |
| `STRIPE_WEBHOOK_SECRET` | type `pending` for now; replaced in Part 4 |
| `SENDGRID_API_KEY` | from Part 2 step 3 |
| `AIRROI_API_KEY` | leave blank, or your key |
| `OPENAI_API_KEY` | leave blank, or your key |
| `BRIEF_LLM_MODEL` | leave blank, or a current OpenAI model name |

4. Click **Apply**. The first build takes about 5 minutes.
5. Open **str-optimizer-api** and copy its address at the top, e.g. `https://str-optimizer-api.onrender.com`. **If Render added extra characters to the name, write the exact address down: you need it in Part 5.**
6. Visit that address + `/health`. You should see `"database": true` and `"stripe": true`.

If Render rejects a plan name in `render.yaml`, pick **Starter** for the three services and the smallest **Basic** database in the dashboard instead.

## Part 4 — Connect Stripe (5 minutes)

1. Stripe Dashboard → Developers → **Webhooks** → **Add endpoint**.
2. Endpoint URL: your Render API address + `/api/stripe/webhook`, e.g. `https://str-optimizer-api.onrender.com/api/stripe/webhook`
3. Select these events:
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.paid`
   - `invoice.payment_failed`
4. Save, then click **Reveal** under Signing secret and copy it (`whsec_…`).
5. Render → **str-optimizer-api** → **Environment** → set `STRIPE_WEBHOOK_SECRET` to that value → **Save changes**. It redeploys by itself.

Test mode and live mode each have their own webhook and signing secret. Use the one that matches your `STRIPE_SECRET_KEY`.

## Part 5 — Put the website on Netlify (5 minutes)

1. If your Render API address differs from `https://str-optimizer-api.onrender.com`, open `Downloads/str-optimizer-launch/website/_redirects` in TextEdit and change the two lines to your address. Save.
2. Netlify → **Add new site** → **Deploy manually** → drag the `website` folder onto the page.
3. Netlify gives a preview address like `https://something.netlify.app`. Open it and check:
   - The page loads and the monthly/annual toggle switches.
   - **Sample brief** opens.
   - `<preview address>/health` shows the same JSON as Render. That proves the forwarding works.
4. Do one test checkout from the preview address (use Stripe test mode and card `4242 4242 4242 4242` if your keys are test keys). The brief should arrive by email within about 10 minutes. After paying, Stripe sends you to `optimizemystr.com/welcome`; until Part 6 is done that address still shows the old site, which is expected.

## Part 6 — Point optimizemystr.com at Netlify (10 minutes, plus waiting)

1. **The day before, if possible:** in GoDaddy DNS, lower the TTL on the `A` record for `@` and the `CNAME` for `www` to 600 seconds.
2. Netlify → your site → **Domain management** → **Add a domain** → `optimizemystr.com`. Netlify shows the exact records to use. **Use what Netlify shows, not values from anywhere else.**
3. In GoDaddy DNS:
   - Change the `@` A record to the value Netlify gave you.
   - Change the `www` CNAME to the value Netlify gave you.
   - Delete the old Firebase A records and the Firebase verification TXT record.
   - **Don't touch MX records or the SendGrid CNAMEs from Part 2.** Those are email.
4. Wait. Netlify issues the security certificate within about an hour. A certificate warning in the meantime is normal.
5. Leave the old Firebase site alone until optimizemystr.com is confirmed working on Netlify. It's your fallback.
6. Once it works, raise the TTLs back to 3600.

## Part 7 — First real signup

Sign up for your own listing on the live site. Check that:

- [ ] Stripe shows the subscription
- [ ] the brief arrives from `briefs@optimizemystr.com` and isn't in spam
- [ ] the **Unsubscribe** link opens a confirmation page
- [ ] **Manage subscription** finds you by email

If a brief doesn't arrive, open Render → **str-optimizer-worker** → **Logs**. A failed brief is listed with its reason.

---

## Still open before you advertise

- **Legal pages** (`/privacy`, `/terms`, `/refunds`): the footer links them, but they aren't written until the business name is settled. Stripe requires a reachable refund policy.
- **Leaked Firebase key:** removed from the files in this branch, but it is still in the repo's history and still valid until the developer who owns the Firebase project revokes it.
- **Make the GitHub repo private** (Settings → General → Danger Zone → Change visibility). Render can still deploy from a private repo.

## How it works

```
Customer pays on optimizemystr.com
  └─ Netlify forwards /api/* ─► Render API ─► Stripe Checkout
Stripe ─► /api/stripe/webhook ─► subscriber saved + brief queued (database)
Render worker ─► reads listing ─► math ─► market data ─► copy ─► email via SendGrid
Render scheduler (daily) ─► queues briefs for anyone 30+ days since their last one
```

Run the tests: `python -m pytest tests` (set `TEST_DATABASE_URL` to a throwaway local Postgres to include the database tests).
