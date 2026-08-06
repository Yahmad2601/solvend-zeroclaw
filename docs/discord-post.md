# SolVend — a physical vending machine that takes USDC over Telegram

**Video (2:5x):** «FILL: link»
**Repo:** «FILL: github.com/you/solvend»
**Custody: T1 — no keys held. Secrets on the box: one RPC key.**

---

A customer DMs the shop's bot: *"cola"*. The agent replies with a Solana Pay QR.
They pay 1.50 USDC from any wallet. ~60 seconds later the bot sends a 4-digit
code. They punch it into the keypad on the machine and a gantry drops the can.

Built on a Raspberry Pi 4 running stock ZeroClaw, driving an ESP32 over USB
serial. No cloud, no Wi-Fi on the machine, no key on the box.

**Who it's for:** the corner shop that already runs its business out of a chat
app and wants to take stablecoins without a payment processor, a POS terminal,
or a merchant account. Setup is an evening. Hardware is a Pi and an ESP32.

## Architecture — Tier 1, deliberately

**Pi** runs ZeroClaw: two Telegram bots, skills, SOP engine, approval
checkpoints. **ESP32** does nothing but report keypresses and obey dispense
commands over a 4-token serial protocol. It has no Wi-Fi stack, no TLS, no API
key. Dump the flash and you get pin numbers.

**Two separate bots, not one.** `telegram.shop` faces customers;
`telegram.operator` receives refund approvals. The separation is structural
rather than a chat-id check — a customer messaging the shop bot has no path to
the approval route at all.

Telegram long-polls, so like WhatsApp web mode it needs no public URL, no
tunnel, and no app review. A shop Pi behind NAT just works. (We started on
WhatsApp; the stock ZeroClaw release doesn't compile the WhatsApp channel, and
swapping the customer channel turned out to be four config lines — see
"component boundaries" below.)

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

**50 tests, mocked RPC, no live network.**

## Detection can't depend on the customer's wallet

The Solana Pay `reference` account is how a merchant normally finds the paying
transaction. Testing on devnet we found **real wallets that silently omit it** —
the payment lands, the money arrives, and the reference lookup returns nothing.
For a vending machine that is the worst possible failure: it takes your money
and dispenses nothing.

So detection doesn't rely on it. Reference lookup runs first; when it finds
nothing, we fall back to reading the merchant's own token account. Crucially
this changed *discovery* only — `validate_transfer` is still the single
authorization gate, so none of the three defenses above move. The fallback is
deliberately stricter than the reference path, because without a reference the
chain states no intent: exact amount only (a 2.50 payment can't satisfy a 1.00
invoice), the payment must be no older than the invoice, and a signature already
spent on another invoice is refused, with a `UNIQUE` index as the backstop.

Verified end to end on devnet against both wallet behaviours: one payment
carrying a reference, one with it stripped, both dispensing correctly.

## Bypassing the blockhash trap

Trap #1 doesn't apply to us. We hold a Solana Pay **URI** across the approval
wait, not a pre-built transaction. A URI is a plain string with no blockhash and
no TTL — the operator can approve after lunch and it's still valid. No durable
nonce account, no 0.0015 SOL rent, no one-nonce-per-pending-approval
serialization. That's a direct payoff of staying at T1.

## Cost

The minute poller is a **cron shell job, not an agentic run**: 1,440 chain
checks a day for **zero tokens**. OTP delivery goes out via `zeroclaw channel
send` — a fixed template with a code from SQL, so no model can hallucinate a
digit or be talked into issuing one. The LLM is woken only for refunds and
expiries, where judgment is genuinely required.

«FILL: actual 30-day model spend»

## Component boundaries we hit (and documented)

Building on a stock release surfaced things worth writing down:

- **The WhatsApp channel isn't compiled into the stock binary** — `channel list`
  reports it "configured, not compiled". Moving the customer channel to Telegram
  cost four config lines and no code, because `solvend.py` contains no channel
  logic at all. The channel is data.
- **Two config sections were silently reset to defaults while `doctor` reported
  zero errors** — an unrecognised sub-table under `http_request` discarded the
  whole section, restoring `allowed_domains = ["*"]` and disabling the SSRF
  guard. The section whose only job is to be a security boundary was the one
  being dropped, and nothing about the running system looked wrong.
- **Omitting `schema_version` makes the provider silently keyless** — the config
  parses, `doctor` calls the model provider valid, and the API key sitting in
  the file is simply never read.

## ZeroClaw features used

Two Telegram channels · skills & skill bundles · SOP engine (cron + manual
triggers) · approval checkpoints with groups/policies · risk profiles
(`excluded_tools`, `approval_route`, `on_no_approver`) · cron shell jobs ·
per-agent memory · `http_request` locked to two hosts

## Built for this

`solvend.py` (state machine + RPC validation, 50 tests) · serial daemon ·
3 skills · 2 SOPs · ESP32 firmware refactor · systemd units · two verifying
Pi deploy scripts. All MIT, all in the repo, config redacted.

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
budget and a 15-minute TTL, not by entropy. When a payment arrives with no
reference, two concurrent invoices for the same amount can't be told apart from
chain data — they're filled oldest-first, which is correct for a single-slot
machine and stated here because it wouldn't be for a bank of them. PIX/BRL
reconciliation is future work, not built.

Reproduce it: «FILL: repo link»#setup — README has the full evening.
