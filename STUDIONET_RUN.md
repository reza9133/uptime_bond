## Studionet end-to-end — PASSED

10/10 finished in 16.8s · ✅ 5 passed · ❌ 0 failed · ⏭ 5 skipped

| Group | Tests | Passed | Failed | Other |
|---|---:|---:|---:|---:|
| e2e | 10 | 5 | 0 | 5 |

### Step-by-step log

| Time | Result | Step | Transaction |
|---|---|---|---|
| 05:35:41 | info | **configuration** settle=resume time=real evidence=live plan_url=https://www.githubstatus.com/api/v2/incidents.json accounts=10 | `` |
| 05:35:43 | ok | **attached to the existing contract** 0x5fb0515ff37BAfDd24cEFB0d56F3B1EE997b84b7 | `` |
| 05:35:44 | warn | **contract balance vs internal accounting** on-chain 319999 == books 110000 (+209999 kept from rejected calls: this network does not refund them) | `` |
| 05:35:45 | info | **epoch was already audited by an earlier run** {"audit_closes_at": "1793481285", "audit_opens_at": "1792275285", "comp_claimed": "0", "comp_per_subscriber": "0", "comp_pool": "0", "downtime_minutes": "0", "ends_at": "1792271685", "epoch": "1", "pe | `` |
| 05:35:48 | info | **alice's claimable amount** {"epochs": [], "total": "0"} | `` |
| 05:35:48 | ok | **no breach: nothing to claim** provider met the target | `` |
| 05:35:55 | ok | **claim with nothing owed** rejected as expected | `0x46b06d41d0c0c1fea3aeb8bfd9553b484dded59ce5db18b96751c08232d4a831` |
| 05:35:56 | info | **final get_stats** {"admin": "0x68ACbA0E5A562e6D03D547366176dC1736De45e3", "contract_balance_expected": "110000", "min_bond": "1000", "paused": "False", "protocol_fee_bps": "100", "total_bond_held": "100000", "total_bre | `` |
| 05:35:58 | warn | **final on-chain balance** on-chain 319999 == books 110000 (+209999 kept from rejected calls: this network does not refund them) | `` |
