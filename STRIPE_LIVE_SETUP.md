# Stripe Live Mode Setup

## Current State
- Mode: **TEST** (Stripe test keys)
- No real money is charged
- Test cards work: 4242 4242 4242 4242

---

## Step 1 — Get Live Keys from Stripe Dashboard

1. Go to [dashboard.stripe.com](https://dashboard.stripe.com)
2. Toggle the top-left switch from **Test mode** to **Live mode**
3. Go to **Developers → API keys**
4. Copy:
   - **Publishable key**: 
   - **Secret key**: 

---

## Step 2 — Update .env on Server

SSH into the server and run:

```bash
nano /root/ugoingviral-backend/.env
```

Replace the Stripe test keys:

```env
# Replace these:
STRIPE_SECRET_KEY=sk_live_YOUR_LIVE_SECRET_KEY
STRIPE_PUBLISHABLE_KEY=pk_live_YOUR_LIVE_PUBLISHABLE_KEY
STRIPE_WEBHOOK_SECRET=whsec_YOUR_LIVE_WEBHOOK_SECRET
```

---

## Step 3 — Create Live Webhook

1. Go to **Stripe Dashboard → Developers → Webhooks**
2. Click **Add endpoint**
3. URL: `https://ugoingviral.com/api/billing/webhook`
4. Select events:
   - `checkout.session.completed`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
5. Copy the **Signing secret** (`whsec_...`) → put in `.env` as `STRIPE_WEBHOOK_SECRET`

---

## Step 4 — Create Live Products in Stripe

In Stripe Dashboard → **Products**, create products matching your plans:

| Plan     | Price (DKK/mo) | Price (USD/mo) |
|----------|---------------|----------------|
| Starter  | 279           | 40             |
| Growth   | 399           | 57             |
| Basic    | 499           | 70             |
| Pro      | 999           | 140            |
| Elite    | 1499          | 210            |
| Personal | 2499          | 350            |
| Agency   | 1399          | 199            |

For each product, copy the **Price ID** (`price_live_...`) and update `routes/billing.py` PLANS dict.

---

## Step 5 — Restart Service

```bash
systemctl restart ugoingviral
systemctl status ugoingviral
```

---

## Step 6 — Pre-Live Checklist

Run through these before announcing to customers:

- [ ] Test checkout with a real card (small amount)
- [ ] Confirm webhook fires in Stripe Dashboard → Webhooks → Recent deliveries
- [ ] Confirm plan updates in user account after checkout
- [ ] Confirm confirmation email is received (requires SendGrid key)
- [ ] Test subscription cancellation flow
- [ ] Verify `/api/billing/plan` returns correct plan after payment
- [ ] Check `.env` has `chmod 600` permissions: `ls -la /root/ugoingviral-backend/.env`
- [ ] Confirm HTTPS is active: `curl -I https://ugoingviral.com | grep -i strict`

---

## Rollback

To go back to test mode, restore the `sk_test_...` keys in `.env` and restart.

```bash
systemctl restart ugoingviral
journalctl -u ugoingviral -n 30 --no-pager | grep -i stripe
```

---

## Notes

- DKK pricing: Stripe charges in the currency set per Price object
- The current code always creates checkout sessions in DKK
- USD prices shown on landing page are reference only (not charged)
- Founding member prices are locked — webhook handler checks `billing.founding_member` flag before updating plan
