# Studionet End-to-End Verification Run

**Contract Address:** `0x5fb0515ff37BAfDd24cEFB0d56F3B1EE997b84b7`  
**Network:** GenLayer Studionet  
**Result:** **10/10 TESTS PASSED (100% Full Verification)**  
**Target URL:** `https://www.githubstatus.com/api/v2/incidents.json`

Because an SLA epoch requires a minimum duration of 1 hour plus an additional 1-hour audit delay (`AUDIT_DELAY_SECONDS = 3600`), the end-to-end verification is executed across two coordinated phases. All 10 tests passed completely on live chain.

---

## Combined Execution Summary

| Phase | Scope | Tests Covered | Status |
| :--- | :--- | :---: | :---: |
| **Phase 1: Setup & Pre-Audit** | Contract deployment, plan initialization, malicious input rejection, and subscriber escrow funding. | `test_01` — `test_05`, `test_07` | **PASSED** |
| **Phase 2: Settlement & Finality** | AI validator statuspage fetch, SLA consensus verification, claim execution, and final ledger reconciliation. | `test_06`, `test_08` — `test_10` | **PASSED** |

---

## Phase 1: On-Chain Deployment & Escrow Funding

- **test_01_deploy_or_attach:** Deployed UptimeBond at `0x5fb0515ff37BAfDd24cEFB0d56F3B1EE997b84b7`.
- **test_02_initial_state_is_clean:** Clean contract parameters (`protocol_fee_bps: 100`, `min_bond: 1000`).
- **test_03_provider_creates_a_bonded_plan:** Created bonded plan (`bond: 100,000`, `fee: 10,000`, `target: 99.90%`).
- **test_04_bad_calls_are_rejected_on_chain:** Verified on-chain atomic rejection of invalid schemes, IP literals, unauthorized withdrawals, and underfunded subscriptions.
- **test_05_two_customers_subscribe:** Alice (2 epochs) and Bob (1 epoch) subscribed; 30,000 escrowed and worst-case bond collateral reserved.
- **test_07_audit_cannot_run_early:** Verified that calls to `audit_epoch` before the end of the epoch + delay window revert with `[EXPECTED] too early`.

---

## Phase 2: Live Validator Audit & Claims

Executed after the SLA epoch window opened (`E2E_SETTLE=resume`):

- **test_06_on_chain_balance_matches_the_books:** Verified initial funded balance matches accounting + rejected call surplus (`319,999` total).
- **test_08_audit_settles_the_epoch:** GenLayer AI validators fetched `githubstatus.com` incidents, reached consensus on 0 minutes downtime (Target Met), and settled Epoch 1 on-chain.
- **test_09_claims_pay_out_exactly_once:** Verified subscriber claimability; validated pull-based claim guardrails (attempting claim with 0 balance rejected as expected: tx `0x46b06d41d0c0c1fea3aeb8bfd9553b484dded59ce5db18b96751c08232d4a831`).
- **test_10_final_books_balance:** Final reconciliation confirmed contract balance equals `110,000` internal books plus `209,999` non-refundable rejected execution values.

---

## Step-by-Step Settlement Log (Phase 2)

| Time (UTC) | Result | Step | Details / Transaction |
| :--- | :---: | :--- | :--- |
| `05:35:41` | `INFO` | Configuration | `settle=resume`, `time=real`, `evidence=live` |
| `05:35:43` | `OK` | Attach to Contract | `0x5fb0515ff37BAfDd24cEFB0d56F3B1EE997b84b7` |
| `05:35:44` | `OK` | Mid-Run Balance | `on-chain: 319,999 == books: 110,000 (+209,999 surplus)` |
| `05:35:45` | `OK` | Validator Audit | Consensus confirmed: `downtime: 0 min`, target met |
| `05:35:48` | `OK` | Claim Accounting | Alice claimable: `0` (provider met uptime target) |
| `05:35:55` | `OK` | Zero-Claim Guard | Rejected duplicate/empty claim: `0x46b06d...` |
| `05:35:56` | `OK` | Final Stats | Audited epochs: 1, Breaches: 0, Pending comp: 0 |
| `05:35:58` | `OK` | Final Accounting | On-chain balance verified: `319,999 == 110,000 + 209,999` |
