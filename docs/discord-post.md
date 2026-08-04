# SolVend — a physical vending machine that takes USDC over WhatsApp

**Video (2:5x):** «FILL: link»
**Repo:** «FILL: github.com/you/solvend»
**Custody: T1 — no keys held. Secrets on the box: one RPC key.**

---

A customer DMs the shop's WhatsApp: *"cola"*. The agent replies with a Solana
Pay QR. They pay 1.50 USDC from any wallet. ~60 seconds later WhatsApp buzzes
with a 4-digit code. They punch it into the keypad on the machine and a gantry
drops the can.

Built on a Raspberry Pi 4 running stock ZeroClaw, driving an ESP32 over USB
serial. No cloud, no Wi-Fi on the machine, no key on the box.

**Who it's for:** the corner shop that already runs its business out of
WhatsApp and wants to take stablecoins without a payment processor, a POS
terminal, or a merchant account. Setup is an evening. Hardware is a Pi and an
ESP32.

## Architecture — Tier 1, deliberately

**Pi** runs ZeroClaw: WhatsApp (web mode) + Telegram, skills, SOP engine,
approval checkpoints. **ESP32** does nothing but report keypresses and obey
dispense commands over a 4-token serial protocol. It has no Wi-Fi stack, no TLS,
no API key. Dump the flash and you get pin numbers.

WhatsApp **web mode**, not Cloud API — an outbound client needs no public URL,
no tunnel, no Meta app review. A shop Pi behind NAT just works.

## Safety

The whole design follows one rule: **the LLM never decides anything that touches
money.** It talks to customers. SQL and integer comparisons do the rest.

Three defenses are structural — they hold *even if the model complies fully
with an attacker*, because the fact each attack needs to control is not a
parameter anywhere in the codebase:

1. **Price** lives in `solvend.ITEMS`, not in prompt text. `cmd_invoice` has no
   `amount` argument. "Charge me 0.01 for a cola" has nothing to bind to.
2. **Refund destination** is derived from `meta.preTokenBalances` of the
   settling transaction — the account whose USDC balance went *down*. There is
   no address parameter to inject into. A test asserts its absence, so a future
   refactor that adds one fails CI.
3. **Reference-key entropy** is `os.urandom(32)`, not model-generated base58.
   An LLM asked for "a random string" produces correlated output; colliding
   references cross-credit one customer's payment to another's drink.

Also: `getSignaturesForAddress` returning a signature is **not** proof of
payment. The reference is a public read-only account — anyone can attach it to
any transaction. We fetch the transaction and check the merchant's USDC balance
delta. Dust payments, wrong-recipient payments, wrong-mint payments and
failed-but-signed transactions all fail to settle, with tests for each.

Refunds: SOP approval checkpoint → operator's Telegram → on approval the agent
emits a **Solana Pay URI** the operator scans with their own wallet.
`on_no_approver = "deny"`. We never hold a key.

**41 tests, mocked RPC, no live network.**

## Bypassing the blockhash trap

Trap #1 doesn't apply to us. We hold a Solana Pay **URI** across the approval
wait, not a pre-built transaction. A URI is a plain string with no blockhash and
no TTL — the operator can approve after lunch and it's still valid. No durable
nonce account, no 0.0015 SOL rent, no one-nonce-per-pending-approval
serialization. That's a direct payoff of staying at T1.

## Cost

The minute poller is a **`[cron.solvend_poll]` shell job, not an agentic run**:
1,440 chain checks a day for **zero tokens**. OTP delivery goes out via
`zeroclaw channel send` — a fixed template with a code from SQL, so no model can
hallucinate a digit or be talked into issuing one. The LLM is woken only for
refunds and expiries, where judgment is genuinely required.

«FILL: actual 30-day model spend, from `zeroclaw` cost tracking»

## ZeroClaw features used

WhatsApp + Telegram channels · skills & skill bundles · SOP engine (cron +
manual triggers) · approval checkpoints with groups/policies · risk profiles
(`excluded_tools`, `approval_route`, `on_no_approver`) · `[cron.*]` shell jobs ·
per-agent memory · `http_request` locked to two hosts

## Built for this

`solvend.py` (state machine + RPC validation, 41 tests) · serial daemon ·
3 skills · 2 SOPs · ESP32 firmware refactor · systemd units. All MIT, all in
the repo, config redacted.

## Prompt-injection transcript

11 attacks, full protocol and results in the repo. The one worth watching in the
video: an attacker asks to refund to *their* address, the operator sees the
checkpoint, **approves it** — and the URI pays the original payer anyway. The
attack completes the entire workflow and still fails.

«FILL: paste the 3–4 strongest transcript excerpts here»

## Honest limits

An operator who approves a bad refund is not stopped by this. Root on the Pi is
game over. Helius can withhold a signature (a stuck invoice — never a stolen
one; it cannot manufacture a payment). A 4-digit OTP is bounded by a 5-attempt
budget and a 15-minute TTL, not by entropy. PIX/BRL reconciliation is future
work, not built.

Reproduce it: «FILL: repo link»#setup — README has the full evening.
