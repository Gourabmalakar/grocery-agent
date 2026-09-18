# Setting up Face ID / Touch ID (WebAuthn + Tailscale)

Not built yet — deferred by choice, not by difficulty, because it needs your
phone and a Tailscale account in the loop, which no amount of code alone can
set up. Everything else in Phase 1 (transaction-sentinel, the evaluator,
the adversarial and sentinel test suites) does not depend on this and is
already in place; passphrase auth covers the same two approval points
(cart, payment) in the meantime.

## Why Tailscale specifically

WebAuthn (the standard behind Face ID and Touch ID on the web) requires a
**secure context** — HTTPS, with an origin the browser trusts — and the
Relying Party ID has to match that origin exactly. `http://127.0.0.1:8800`
satisfies this by a `localhost` exception on the Mac itself, but your phone
on the same Wi-Fi hitting `http://192.168.x.x:8800` does not: no exception,
and no certificate.

[Tailscale](https://tailscale.com) solves both at once: `tailscale serve`
gives the Console a stable `*.ts.net` hostname with a real Let's Encrypt
certificate, reachable only from your own devices on your tailnet — never
exposed publicly.

## What you'll need to do (once you're ready)

1. Install Tailscale on your Mac and sign in:
   ```bash
   brew install tailscale
   sudo tailscale up
   ```
2. Install the Tailscale app on your phone (App Store / Play Store) and sign
   into the **same account**.
3. From the repo root, with the Console running on :8800:
   ```bash
   tailscale serve https / http://127.0.0.1:8800
   ```
   This prints a `https://<your-machine>.<your-tailnet>.ts.net` URL.
4. Tell me that URL (or set it as `CONSOLE_URL` yourself) and ask me to wire
   up `console/auth/webauthn.py` — registration and assertion routes, a
   passkey-required decorator on the approval endpoints, and a QR code or
   link you can open on your phone to register its Face ID.
5. Register a passkey from the Mac (Touch ID) and from your phone (Face ID)
   the first time each device visits the Console.

## What changes once this lands

Per the plan's authentication table:

| Step | Factor |
|---|---|
| Cart approval | passkey |
| Payment, under ₹1,000 | passkey |
| Payment, ₹1,000+ or first order at a new merchant | passkey **+** passphrase |

Passphrase-only auth (today's state) does not go away — it stays as the
factor that gates clearing PANIC and rotating the passphrase itself, exactly
as the plan describes.
