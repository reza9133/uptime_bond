# UptimeBond

**Collateral-backed SLAs for online services, audited by GenLayer validators.**

> Status: the contract and its test suite (241 tests, including an
> [adversarial suite](#adversarial-tests)) are written for GenLayer's Direct
> Mode; re-run `genvm-lint check` and `genvm-lint typecheck` after any change.
> A live end-to-end test for Studionet is in [`tests_e2e/`](#one-command-runs-and-the-live-report).
> Run everything, with a live report, using `bash scripts/run_all.sh`.
>
> Studionet contract address: `0xE938B4c6A38Eb347e100b868fB9F5B4c7679E2D6`

A provider publishes a plan: *"this service is up at least 99.9% of every
epoch"*, priced per epoch, with a public status page as the source of truth,
and locks a **bond** in the contract. Customers prepay for epochs and their fees
sit in escrow, not in the provider's pocket. When an epoch ends, **anyone** can
call `audit_epoch`. GenLayer validators independently read the status page,
measure the outage inside that epoch, and must agree on the same **compensation
tier** before any money moves:

* target met → the provider is paid the epoch's fees;
* target missed → every subscriber is refunded part of their fee from escrow,
  and for the worse tiers an extra penalty from the provider's bond. The
  provider keeps the rest;
* nobody could produce usable evidence in time → the epoch **lapses** and all
  fees are refunded. No evidence, no revenue.

## Why this needs GenLayer

SLA credits today are a manual, adversarial process: the customer has to notice
the outage, file a ticket, and argue with the vendor that wrote the status page.
A plain smart contract can't read a status page. A single oracle can be bribed
or shut down. Here, validators with different infrastructure fetch the evidence
themselves and have to converge on the same outcome, with consensus reached on
the *derived tier*, not on a raw number that two validators could read
differently on either side of a boundary (the
["compare derived status, not raw data"](https://docs.genlayer.com/developers/intelligent-contracts/crafting-prompts#compare-derived-status-not-raw-data)
pattern).

## How it works

```
 provider                                           customers
    │ create_plan(terms, evidence URLs) + BOND         │
    ▼                                                   │ subscribe(plan, n epochs) + FEES
 ┌────────────────────────────── contract ──────────────▼───────────────────────┐
 │  bond (1 fee reserved per paid subscriber-epoch)      escrow (fees for unsettled epochs)  │
 └───────┬────────────────────────────────────────────────────────┬──────────────┘
         │ epoch ends (+1h for status pages to catch up)          │
         ▼                                                        │
   audit_epoch(plan, epoch)   ◄── anyone, within 14 days          │
   validators fetch evidence, measure outage, agree on a TIER     │
         │                                                        │
   ┌─────┴──────────────┬───────────────────────────────┐         │
   ▼                    ▼                               ▼         │
 tier 0              tiers 1–4                  no usable evidence in 14 days
 provider paid       refund (escrow) + penalty  lapse_epoch(): full refund,
 in full             (bond) per subscriber;     provider gets nothing,
                     provider keeps the rest    bond untouched
                          │
                          ▼
              claim_compensation / claim_all_compensation (pull-based)
```

### Tiers

An epoch's measured uptime is compared with the plan's target. The shortfall
(in basis points) picks the tier. Both columns are a share of one subscriber's
fee for that epoch:

| Tier | Shortfall vs. target | Refund (from escrow) | Penalty (from bond) |
|---|---|---|---|
| 0 | none (target met) | 0% | 0% |
| 1 | up to 1.00% | 10% | 0% |
| 2 | up to 5.00% | 25% | 5% |
| 3 | up to 20.00% | 50% | 25% |
| 4 | worse than that | 100% | 100% |
| lapsed | no usable evidence | 100% | 0% |

`quote_outcome(plan_id, downtime_minutes)` returns the tier a given amount of
outage would produce for a plan.

### How outage is measured

Each plan names a `status_url` and, optionally, an independent `monitor_url`.
Both are fixed at creation and public before anyone subscribes.

1. **Statuspage-style incident feeds** (`{"incidents": [...]}`, as served by
   Atlassian Statuspage at `/api/v2/incidents.json`) are computed
   *deterministically in Python*, with no LLM involved. Incidents are clipped to
   the epoch window, only `major` and `critical` impact counts, overlaps are
   merged, and ongoing incidents (no `resolved_at`) count until the window ends. An
   incident whose `resolved_at` is present but unreadable rejects the audit rather
   than being guessed at. If a full feed
   page (50 entries) doesn't reach back to the start of the window, the audit is
   rejected instead of guessing.
2. **Any other page** goes through an LLM prompt that must answer with
   `{"coverage": "full" | "partial" | "none", "downtime_minutes": n}`. Anything
   but `"full"` coverage is rejected. The page is treated as untrusted data.
3. With **two sources**, the stricter reading (more downtime) wins, so a clean
   status page can't hide an outage an independent monitor saw. If either source
   is unusable, the audit is rejected.

A rejected audit changes nothing and can simply be retried; it never defaults to
a verdict.

### Consensus

The leader derives `{tier, downtime_minutes}`. Every validator repeats the work
independently and agrees only if its **tier matches exactly** and its minutes are
within a small tolerance (`max(15, 1% of the epoch)`). If the leader found the
evidence unusable and the validator does too, they agree to fail the call
together rather than rotating forever; if only one side fails, the result is
rejected.

### Guardrails

* **Cover starts next epoch.** `subscribe` always begins in the following epoch,
  so nobody can buy cover once an outage has started.
* **The bond can't be oversold.** Every paid epoch reserves one full fee of the
  provider's bond (the largest penalty any tier applies). `subscribe` fails if
  the free bond can't cover it; `withdraw_bond` can only take the free part.
  Reservations are released as epochs settle.
* **Fees are escrowed.** The provider is paid only when an epoch settles.
* **Pull-based compensation.** Settlement cost never grows with the number of
  subscribers; each subscriber claims their own share, once per epoch.
* **Anyone can settle**, so a forgetful provider or customer can't stall funds.
* **Never trapped.** Pausing blocks only new plans and subscriptions. Audits,
  lapses, claims and bond withdrawals keep working.
* **URL hygiene.** Evidence URLs must be `https`, ASCII, without credentials,
  ports, IP literals or private-looking hosts, and end in a real-looking TLD.
  Evidence pages larger than 2 MB are rejected.
* **No `__receive__`.** A bare transfer is rejected, so every unit the contract
  holds arrives through a payable method and is booked in
  `get_stats().contract_balance_expected`. One exception is network-level, not
  contract-level: see "Value sent with a rejected call" under Honest limitations.

## Contract API

All numbers inside returned dictionaries are decimal strings.

| Method | Type | Notes |
|---|---|---|
| `create_plan(name, status_url, monitor_url, target_uptime_bps, epoch_seconds, fee_per_epoch)` | payable write | value sent = initial bond; `monitor_url` may be `""`; returns the plan id |
| `top_up_bond(plan_id)` | payable write | provider only |
| `withdraw_bond(plan_id, amount)` | write | provider only; free (unreserved) bond only |
| `set_plan_accepting(plan_id, accepting)` | write | provider only; stops or resumes new subscriptions |
| `subscribe(plan_id, epochs)` | payable write | value must be exactly `fee × epochs`; 1–24 epochs; returns the subscription id |
| `audit_epoch(plan_id, epoch)` | write | anyone, from epoch end + 1h until +14 days; returns `MET` or `BREACH_T1`…`BREACH_T4` |
| `lapse_epoch(plan_id, epoch)` | write | anyone, once the 14-day audit window has closed |
| `claim_compensation(subscription_id, epoch)` | write | pays the subscriber, whoever calls it |
| `claim_all_compensation(subscription_id)` | write | every settled, unclaimed epoch of that subscription |
| `set_protocol_fee_bps` / `set_treasury` / `set_min_bond` / `set_paused` / `transfer_admin` | write, admin | protocol fee is capped at 10% and only applies to provider revenue |
| `get_plan(id)` / `get_plans(start, count)` / `get_provider_plans(wallet)` | view | |
| `get_subscription(id)` / `get_wallet_subscriptions(wallet)` / `get_claimable(id)` | view | |
| `get_current_epoch(plan_id)` / `get_epoch(plan_id, epoch)` | view | timings, status, tier, payouts |
| `quote_outcome(plan_id, downtime_minutes)` / `get_tier_table()` | view | |
| `get_audit_log(start, count)` / `get_stats()` | view | |

Plan limits: target uptime 90.00%–99.99%, epoch length 1 hour–31 days (whole
hours), fee per epoch at least 10,000 units. Amounts are in the chain's smallest
unit.

`genvm-lint schema contracts/uptime_bond.py` prints the machine-readable version.

## Running the tests

[`genlayer-test`](https://docs.genlayer.com/api-references/genlayer-test) Direct
Mode runs the contract's actual Python code in-process: no Docker, no GenVM, no
network.

```bash
pip install -r requirements.txt
pytest tests/ -v
```

The first run downloads and caches the pinned GenVM release (`v0.2.12`, matching
the `Depends` header in the contract) under `~/.cache/gltest-direct/`.

Direct Mode doesn't move balances, so the suite captures every transfer the
contract emits and every payable call's value, and after **each test** checks
that value-in minus value-out equals the contract's own accounting. The 241 tests
cover:

* plan, bond and subscription validation, including unsafe-URL rejection;
* every tier, with exact-edge checks at the 0/100/500/2000 bps boundaries;
* incident handling: impact filtering, overlap merging, clipping, ongoing
  incidents, timestamp formats, junk entries, incomplete feeds;
* the LLM fallback: prompt contents, untrusted-page handling, partial coverage,
  malformed, non-finite and out-of-range answers;
* rejected evidence (HTTP errors, unreachable, empty, oversized, nested-JSON
  bombs) never finalising anything, followed by a successful retry;
* the stricter-of-two-sources rule;
* leader/validator behaviour: agreement, tolerance, tier mismatch, unusable
  evidence on one or both sides;
* lapsing, claiming, double-claim prevention, protocol fees and admin controls;
* a full multi-plan scenario that drains the contract to exactly zero, and a
  seeded randomised run of 150 operations that re-checks the ledger and the
  bond-reservation invariants after every step.

### Adversarial tests

Everything marked `@pytest.mark.adversarial` (run alone with `pytest tests -m adversarial`)
encodes an attack the contract claims to survive:

* **hostile evidence pages**: markup that made the old HTML cleaner quadratic (a 2 MB page of
  `<<<<…` could keep an audit from ever finishing), pages far larger than the prompt, JSON
  bombs, 15,000-incident feeds, duplicated incidents, year 1 / year 9999 timestamps, feeds
  padded with junk to look complete, unreadable `resolved_at` values;
* **URL smuggling**: 20 ways of hiding a private or malformed target in `status_url` /
  `monitor_url` (userinfo tricks, short/decimal IPv4, IPv4-mapped IPv6, trailing dots,
  NUL/DEL bytes, look-alike dots, wrong-case scheme…);
* **hostile callers**: nobody, including the admin, can touch another provider's bond;
  buying cover during an outage pays nothing for that outage; one plan's breach never pays
  another plan's subscribers (checked in both directions); `1:11` and `11:1` style ids can
  never alias a storage record;
* **hostile numbers**: awkward fees with a 10% protocol cut, 2^200-sized fees, absurd view
  arguments;
* **atomicity**: twenty different rejected calls, and rejected audits caused by hostile
  evidence, leave not a single stored number changed;
* **a stateful fuzz** (10 seeds) that mixes hostile evidence into random operations and
  re-checks the ledger, bond reservations and atomicity after every step.

Each of these was checked against a deliberately re-introduced vulnerability (mutation
testing) to make sure it fails when it should.

### One-command runs and the live report

```bash
bash scripts/run_all.sh              # fast suite + Studionet end-to-end
bash scripts/run_all.sh unit         # fast suite only
bash scripts/run_all.sh adversarial  # only the hostile-input tests
bash scripts/run_all.sh e2e          # only the live Studionet test
```

In a GitHub Codespace (the repo ships a `.devcontainer/`), open the **Ports** tab and open
port 8000 ("Live test report"): the page refreshes itself every 3 seconds while tests run
and shows progress, per-group totals, failures, and, for the end-to-end run, a step-by-step
log with every transaction hash. The same data is left in `reports/<suite>/report.html`,
`report.md` and `report.json` (and appended to the job summary in GitHub Actions; see
`.github/workflows/tests.yml`).

The end-to-end test (`tests_e2e/test_studionet_e2e.py`, run through
`gltest --network studionet`) deploys the contract, creates a bonded plan, checks that bad
calls are rejected on chain, subscribes two customers, checks bond reservation and the
on-chain balance, and then audits, claims and re-checks the books. Because an epoch is at
least an hour and an audit opens an hour after it ends, settlement is time-gated:

| `E2E_SETTLE` | behaviour |
|---|---|
| `skip` (default) | do everything possible now, save `reports/studionet/e2e_state.json`, stop |
| `resume` | later (about 3 hours on), audit, claim and verify the saved run |
| `wait` | keep the run alive until the audit opens, then continue |

The first time-gated run stops after the setup tests (the audit tests are skipped, not failed). Once the printed audit time has passed, finish the same run with `E2E_SETTLE=resume bash scripts/run_all.sh e2e`.

`E2E_CONTRACT_ADDRESS=<address>` attaches to an already deployed (fresh) contract instead of deploying one; the author's Studionet instance is `0xE938B4c6A38Eb347e100b868fB9F5B4c7679E2D6`.

`E2E_TIME=warp` jumps the clock with `sim_increaseTime` (GLSim / local simulator only) and
`E2E_EVIDENCE=mock` feeds validators a scripted incident feed, which makes the numbers exact
(10 minutes down in a 60-minute epoch is tier 3: 7,500 per subscriber, 9,900 to the provider).
With the defaults, validators read the real status page (`E2E_PLAN_URL`).

Static checks, if you have [`genvm-linter`](https://docs.genlayer.com/api-references/genlayer-linter):

```bash
genvm-lint check contracts/uptime_bond.py
genvm-lint typecheck contracts/uptime_bond.py
```

## Deploying to Studionet

Constructor arguments:

| Argument | Meaning | Example |
|---|---|---|
| `protocol_fee_bps` | cut of provider revenue sent to the treasury (max 1000 = 10%) | `100` |
| `min_bond` | smallest bond a plan may be created with, in the smallest unit | `1000` |

**GenLayer Studio:** open the Studio, choose the Studionet network, paste
`contracts/uptime_bond.py` into a new contract file, set the two constructor
arguments above and deploy. Payable calls (`create_plan`, `subscribe`,
`top_up_bond`) need a value, which the Studio's write form accepts.

**CLI:**

```bash
npm install -g genlayer
genlayer network set studionet
genlayer deploy --contract contracts/uptime_bond.py --args 100 1000
```

Non-payable calls look like `genlayer write <address> audit_epoch --args 1 1`
and reads like `genlayer call <address> get_plan --args 1` (`<address>` is your
deployed contract, for example `0xE938B4c6A38Eb347e100b868fB9F5B4c7679E2D6` below). For sending a value
with payable calls from the CLI, check `genlayer write --help` for your CLI
version.

### Deployed instance

| Network | Address |
|---|---|
| Studionet | `0xE938B4c6A38Eb347e100b868fB9F5B4c7679E2D6` |

Call it from the CLI (after `genlayer network set studionet`):

```bash
genlayer call 0xE938B4c6A38Eb347e100b868fB9F5B4c7679E2D6 get_stats
genlayer call 0xE938B4c6A38Eb347e100b868fB9F5B4c7679E2D6 get_plan --args 1
genlayer write 0xE938B4c6A38Eb347e100b868fB9F5B4c7679E2D6 audit_epoch --args 1 1
```

To run the live end-to-end test against this instance instead of deploying a new
one, set `E2E_CONTRACT_ADDRESS=0xE938B4c6A38Eb347e100b868fB9F5B4c7679E2D6`. The test expects a fresh contract
(no plans yet, deployed with `100 1000`) and fails with a clear message otherwise.

### A short end-to-end demo

1. As a provider, `create_plan` with `status_url` set to a Statuspage-style
   incident feed (for example `https://www.githubstatus.com/api/v2/incidents.json`),
   `epoch_seconds = 3600` (the shortest allowed), `target_uptime_bps = 9990`,
   `fee_per_epoch = 10000`, and a bond of `100000`.
2. As a customer, `subscribe(plan_id, 2)` with value `20000`. Cover begins at the
   next hour boundary of the plan.
3. After that epoch ends and the one-hour delay passes, call
   `audit_epoch(plan_id, epoch)` from any account.
4. Read `get_epoch(plan_id, epoch)` for the tier and payouts, then
   `claim_all_compensation(subscription_id)` if a refund is due.

This end-to-end flow has been exercised only in Direct Mode with mocked web
pages and LLM answers. Nothing here has been run against a live Studionet or a
live status page yet.

## Honest limitations

* **The evidence is only as honest as its source.** A provider controls its own
  status page and could edit history. The mitigations are an immutable,
  public-before-purchase URL, an optional independent `monitor_url` where the
  stricter reading wins, and the fact that anyone can audit as soon as the
  one-hour delay passes. Customers should prefer plans whose pages they trust.
* **The LLM fallback is approximate.** Deterministic parsing is used wherever the
  page is a Statuspage-style feed; for any other page, validators must read the
  same prose the same way, and borderline cases near a tier edge may need
  rotation. The tier-first consensus and the minute tolerance reduce this, they
  don't eliminate it.
* **The feed-coverage check assumes pages of 50 incidents**, as Statuspage
  serves. A feed with a different page size could under-report old epochs; it is
  safest to audit promptly.
* **Value sent with a rejected call can stay in the contract.** The live
  Studionet end-to-end run showed that when a payable call is rejected (for
  example `subscribe` with the wrong amount, or `create_plan` with an unsafe URL),
  the contract's state is untouched but the value attached to that transaction is
  not refunded: the on-chain balance exceeded the books by exactly the value the
  rejected calls carried (209,999 in that run). Nothing in `get_stats()` counts
  it, no plan can use it, and this version has no function to recover it, so a
  user who mis-sends a payable call loses that amount. Check the amount before
  you send. The e2e test accepts exactly this surplus and fails on any other
  difference; a future version could add an admin sweep limited to
  `balance - contract_balance_expected`.
* **An unfetchable source is a refund, not a pass.** If a status page stays
  unusable for the whole 14-day audit window, subscribers are refunded in full.
  This is deliberate (no evidence, no revenue), but a third-party outage can
  therefore cost an innocent provider an epoch's revenue.
* **Subscriptions can't be cancelled** and plan terms can't be edited once
  created; a provider who wants different terms creates a new plan.
* **Amounts round down** in the provider's favour, by less than one unit per
  subscriber-epoch.
* **The admin is a single trusted key** that sets the protocol fee, treasury,
  minimum bond and pause switch. It cannot touch bonds, escrow or settlements.
  Use a multisig in production.
* **This is a demonstration, not a licensed insurance or financial product.**

## License

MIT
