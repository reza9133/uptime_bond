"""End-to-end test of UptimeBond on a live GenLayer network (Studionet by default).

Run it with:   gltest --network studionet tests_e2e -v      (or: bash scripts/run_all.sh e2e)

The tests run in order and share one deployed contract. Every on-chain action is written to the
live report (reports/studionet/report.html) with its transaction hash.

The contract's time rules are the hard part of testing it on a live chain: an epoch is at least
one hour and an audit opens an hour after the epoch ends, so settlement needs ~3 real hours.
Three environment variables choose how to deal with that:

    E2E_SETTLE    skip   (default) do everything that is possible right now, save state, and stop;
                         run again later with E2E_SETTLE=resume to audit, claim and verify
                  wait   keep the run alive until the audit opens (about 3 hours), then continue
                  resume continue a saved run (reports/studionet/e2e_state.json)
    E2E_TIME      real   (default) the network's clock
                  warp   jump the clock with sim_increaseTime (GLSim / local simulator only)
    E2E_CONTRACT_ADDRESS  attach to this already deployed, fresh contract instead of deploying
                  a new one (it must have been deployed with the constructor arguments 100 1000)
    E2E_EVIDENCE  live   (default) validators fetch E2E_PLAN_URL from the real internet
                  mock   validators are given a scripted incident feed (deterministic numbers;
                         needs a Studio-based network that supports mock validators)

Written against the documented genlayer-test 0.29 Studio-mode API. It has NOT been executed
against a live network by its author, so the helpers below are defensive and report anything
unexpected instead of hiding it.
"""

import datetime as dt
import json
import os
import pathlib
import time
import urllib.request

import pytest
from gltest import get_contract_factory
from gltest.assertions import tx_execution_failed, tx_execution_succeeded

from tools.live_report import step

pytestmark = pytest.mark.e2e

SETTLE = os.getenv("E2E_SETTLE", "skip").lower()
TIME = os.getenv("E2E_TIME", "real").lower()
EVIDENCE = os.getenv("E2E_EVIDENCE", "live").lower()
RPC_URL = os.getenv("E2E_RPC_URL", "http://127.0.0.1:4000/api")
CONTRACT_ADDRESS = os.getenv("E2E_CONTRACT_ADDRESS", "").strip()
PLAN_URL = os.getenv(
    "E2E_PLAN_URL",
    "https://www.githubstatus.com/api/v2/incidents.json"
    if EVIDENCE == "live" else "https://status.example.com/api/v2/incidents.json",
)
WAIT_INTERVAL = int(os.getenv("E2E_WAIT_INTERVAL_MS", "3000"))
WAIT_RETRIES = int(os.getenv("E2E_WAIT_RETRIES", "100"))
STATE_FILE = pathlib.Path(os.getenv("LIVE_REPORT_DIR", "reports/studionet")) / "e2e_state.json"

PROTOCOL_FEE_BPS, MIN_BOND = 100, 1_000
FEE, BOND, EPOCH_SECONDS, TARGET_BPS = 10_000, 100_000, 3_600, 9_990
HOUR = 3_600
CTX = {}   # shared between the ordered tests: contract, plan_id, subscription ids, transaction context


# ---------------------------------------------------------------------------
# helpers (defensive: the exact receipt/contract shapes differ slightly between releases)
# ---------------------------------------------------------------------------


def _factory():
    last = None
    for name in ("UptimeBond", "uptime_bond"):
        try:
            return get_contract_factory(name)
        except Exception as exc:  # noqa: BLE001 - try the next spelling
            last = exc
    raise last


def _tx_hash(receipt) -> str:
    for key in ("hash", "tx_hash", "txHash", "transactionHash", "transaction_hash", "id"):
        value = receipt.get(key) if isinstance(receipt, dict) else getattr(receipt, key, None)
        if value:
            return str(value)
    return ""


def _bound(contract, account):
    """The same contract, acting as `account` (when the library supports connect())."""
    return contract.connect(account) if hasattr(contract, "connect") else contract


def _write(method, args, account, value=0, name=None):
    """Send a state-changing transaction and return its receipt."""
    contract = CTX["contract"]
    target = _bound(contract, account)
    fn = getattr(target, method)
    try:
        prepared = fn(args=list(args)) if hasattr(contract, "connect") else fn(args=list(args), account=account)
    except TypeError:
        prepared = fn(args=list(args))
    if not hasattr(prepared, "transact"):          # older releases return the receipt directly
        return prepared
    kwargs = dict(value=value, consensus_max_rotations=3, wait_interval=WAIT_INTERVAL, wait_retries=WAIT_RETRIES)
    if CTX.get("tx_context"):
        kwargs["transaction_context"] = CTX["tx_context"]
    return prepared.transact(**kwargs)


def _view(method, *args):
    fn = getattr(CTX["contract"], method)
    return fn(args=list(args)).call()


def _succeeds(label, method, args, account, value=0, detail=""):
    receipt = _write(method, args, account, value)
    ok = bool(tx_execution_succeeded(receipt))
    step(label, "ok" if ok else "fail", detail or f"{method}{tuple(args)}", tx=_tx_hash(receipt))
    assert ok, f"{label}: transaction did not succeed ({method})"
    return receipt


def _is_rejected(label, method, args, account, value=0):
    """A transaction the contract must refuse. Returns True when it did (reverted or raised)."""
    try:
        receipt = _write(method, args, account, value)
    except Exception as exc:  # noqa: BLE001 - some releases raise on a failed execution
        step(label, "ok", f"rejected as expected ({type(exc).__name__}: {str(exc)[:160]})")
        return True
    rejected = bool(tx_execution_failed(receipt))
    step(label, "ok" if rejected else "fail",
         "rejected as expected" if rejected else "NOT rejected: the contract accepted a bad call", tx=_tx_hash(receipt))
    return rejected


def _stats():
    return dict(_view("get_stats"))


def _rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _balance(gl_client, address):
    for call in (lambda: gl_client.get_balance(address), lambda: gl_client.get_balance(address=address)):
        try:
            return int(call())
        except Exception:  # noqa: BLE001
            continue
    return None


def _save_state(extra=None):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {k: CTX[k] for k in ("address", "plan_id", "alice_sub", "bob_sub", "epoch", "audit_opens_at") if k in CTX}
    data.update(extra or {})
    STATE_FILE.write_text(json.dumps(data, indent=1))


def _need(*keys):
    missing = [k for k in keys if k not in CTX]
    if missing:
        pytest.skip(f"an earlier step did not complete (missing {', '.join(missing)})")


def _mock_context(starts_at):
    """Validators that all see the same scripted incident feed: a 10-minute major outage."""
    from gltest import get_validator_factory
    iso = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    feed = {"incidents": [{"impact": "major", "started_at": iso(starts_at + 60), "resolved_at": iso(starts_at + 660)}]}
    mock_web = {"nondet_web_request": {PLAN_URL: {"method": "GET", "status": 200, "body": json.dumps(feed)}}}
    validators = get_validator_factory().batch_create_mock_validators(count=5, mock_web_response=mock_web)
    return {"validators": [v.to_dict() for v in validators]}


# ---------------------------------------------------------------------------
# the ordered scenario
# ---------------------------------------------------------------------------


def test_01_deploy_or_attach(accounts):
    if len(accounts) < 3:
        pytest.skip("need three funded test accounts (provider, two subscribers)")
    step("configuration", "info",
         f"settle={SETTLE} time={TIME} evidence={EVIDENCE} plan_url={PLAN_URL} accounts={len(accounts)}")
    factory = _factory()
    if SETTLE == "resume":
        if not STATE_FILE.exists():
            pytest.fail(f"E2E_SETTLE=resume but {STATE_FILE} does not exist; run the setup phase first")
        CTX.update(json.loads(STATE_FILE.read_text()))
        CTX["contract"] = factory.build_contract(CTX["address"], accounts[0])
        step("attached to the existing contract", "ok", CTX["address"])
    elif CONTRACT_ADDRESS:
        if not (len(CONTRACT_ADDRESS) == 42 and CONTRACT_ADDRESS.startswith("0x")
                and all(c in "0123456789abcdefABCDEF" for c in CONTRACT_ADDRESS[2:])):
            pytest.fail(f"E2E_CONTRACT_ADDRESS is not a 0x-prefixed 20-byte address: {CONTRACT_ADDRESS!r}")
        CTX["contract"], CTX["address"] = factory.build_contract(CONTRACT_ADDRESS, accounts[0]), CONTRACT_ADDRESS
        step("attached to the contract given in E2E_CONTRACT_ADDRESS", "ok", CONTRACT_ADDRESS)
    else:
        contract = factory.deploy(args=[PROTOCOL_FEE_BPS, MIN_BOND], account=accounts[0], consensus_max_rotations=3)
        CTX["contract"], CTX["address"] = contract, str(contract.address)
        step("deployed UptimeBond", "ok", f"address {CTX['address']}")
    assert CTX["address"]


def test_02_initial_state_is_clean():
    _need("contract")
    if SETTLE == "resume":
        pytest.skip("resuming: initial state was verified in the setup run")
    stats = _stats()
    step("get_stats on a fresh contract", "info", json.dumps(stats))
    assert stats["total_plans"] == "0" and stats["paused"] in ("False", "false", False)
    assert stats["protocol_fee_bps"] == str(PROTOCOL_FEE_BPS) and stats["min_bond"] == str(MIN_BOND)
    assert stats["contract_balance_expected"] == "0"


def test_03_provider_creates_a_bonded_plan(accounts):
    _need("contract")
    if SETTLE == "resume":
        pytest.skip("resuming: plan already created")
    _succeeds("create_plan (bond posted)", "create_plan",
              ["E2E plan", PLAN_URL, "", TARGET_BPS, EPOCH_SECONDS, FEE], accounts[0], value=BOND)
    CTX["plan_id"] = int(_stats()["total_plans"])
    plan = dict(_view("get_plan", CTX["plan_id"]))
    step("plan stored on chain", "info", json.dumps(plan))
    assert plan["bond_total"] == str(BOND) and plan["bond_reserved"] == "0"
    assert plan["target_uptime_bps"] == str(TARGET_BPS) and plan["accepting"] in (True, "True", "true")
    assert _stats()["total_bond_held"] == str(BOND)


def test_04_bad_calls_are_rejected_on_chain(accounts):
    _need("plan_id")
    if SETTLE == "resume":
        pytest.skip("resuming: rejection checks ran in the setup run")
    before = _stats()
    pid = CTX["plan_id"]
    results = [
        _is_rejected("create_plan with an http:// status URL", "create_plan",
                     ["Bad", "http://status.example.com/x", "", TARGET_BPS, EPOCH_SECONDS, FEE], accounts[0], BOND),
        _is_rejected("create_plan pointing at an IP address", "create_plan",
                     ["Bad", "https://127.0.0.1/x", "", TARGET_BPS, EPOCH_SECONDS, FEE], accounts[0], BOND),
        _is_rejected("subscribe with the wrong payment", "subscribe", [pid, 1], accounts[1], FEE - 1),
        _is_rejected("a stranger withdraws the provider's bond", "withdraw_bond", [pid, 1], accounts[2]),
        _is_rejected("audit before the window exists", "audit_epoch", [pid, 1], accounts[2]),
    ]
    after = _stats()
    step("state after five rejected calls", "info", "unchanged" if before == after else "CHANGED")
    assert all(results)
    assert before == after, "a rejected transaction changed contract state"


def test_05_two_customers_subscribe(accounts):
    _need("plan_id")
    if SETTLE == "resume":
        pytest.skip("resuming: subscriptions already exist")
    pid = CTX["plan_id"]
    _succeeds("alice subscribes for 2 epochs", "subscribe", [pid, 2], accounts[1], value=2 * FEE)
    CTX["alice_sub"] = int(_stats()["total_subscriptions"])
    _succeeds("bob subscribes for 1 epoch", "subscribe", [pid, 1], accounts[2], value=FEE)
    CTX["bob_sub"] = int(_stats()["total_subscriptions"])

    alice = dict(_view("get_subscription", CTX["alice_sub"]))
    first = int(alice["first_epoch"])
    CTX["epoch"] = first
    epoch = dict(_view("get_epoch", pid, first))
    CTX["audit_opens_at"] = int(epoch["audit_opens_at"])
    step("epoch 1 is funded", "info", json.dumps(epoch))
    plan = dict(_view("get_plan", pid))
    assert first == 1 and epoch["subscribers"] == "2"
    assert plan["bond_reserved"] == str(3 * FEE), "bond must be reserved for every purchased epoch"
    stats = _stats()
    assert stats["total_escrowed_fees"] == str(3 * FEE)
    assert stats["contract_balance_expected"] == str(BOND + 3 * FEE)
    opens = dt.datetime.fromtimestamp(CTX["audit_opens_at"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    step("audit of epoch 1 opens at", "info", opens)
    _save_state()


def test_06_on_chain_balance_matches_the_books(gl_client):
    _need("address")
    actual = _balance(gl_client, CTX["address"])
    expected = int(_stats()["contract_balance_expected"])
    if actual is None:
        step("contract balance vs internal accounting", "warn",
             f"balance query not available in this client; internal books say {expected}")
        pytest.skip("balance query not supported by this genlayer-test release")
    step("contract balance vs internal accounting", "ok" if actual == expected else "fail",
         f"on-chain {actual}, books {expected}")
    assert actual == expected


def test_07_audit_cannot_run_early(accounts):
    _need("plan_id", "epoch")
    if SETTLE != "resume" and TIME == "real":
        assert _is_rejected("audit_epoch before it opens (epoch 1)", "audit_epoch",
                            [CTX["plan_id"], CTX["epoch"]], accounts[2])
    else:
        pytest.skip("the audit window is reached in this mode; nothing to check")


def _wait_for_audit_window():
    opens = CTX["audit_opens_at"]
    if TIME == "warp":
        jump = max(0, opens - int(time.time())) + 120
        reply = _rpc("sim_increaseTime", [jump])
        step("clock advanced with sim_increaseTime", "ok" if "error" not in reply else "fail", json.dumps(reply)[:300])
        assert "error" not in reply, "sim_increaseTime is only available on GLSim / the local simulator"
        return
    remaining = opens + 90 - time.time()
    if remaining <= 0:
        return
    if SETTLE != "wait":
        when = dt.datetime.fromtimestamp(opens, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        step("settlement is time-gated", "warn",
             f"audit opens {when}. Run again then with E2E_SETTLE=resume (state is saved in {STATE_FILE})")
        pytest.skip(f"audit window opens at {when}; re-run with E2E_SETTLE=resume")
    step("waiting for the audit window", "info", f"{int(remaining // 60)} minutes")
    while time.time() < opens + 90:
        time.sleep(min(60, max(1, opens + 90 - time.time())))


def test_08_audit_settles_the_epoch(accounts):
    _need("plan_id", "epoch", "audit_opens_at")
    _wait_for_audit_window()
    pid, epoch = CTX["plan_id"], CTX["epoch"]
    if EVIDENCE == "mock":
        CTX["tx_context"] = _mock_context(int(dict(_view("get_epoch", pid, epoch))["starts_at"]))
    _succeeds("audit_epoch (validators read the evidence)", "audit_epoch", [pid, epoch], accounts[2])
    rec = dict(_view("get_epoch", pid, epoch))
    step("epoch settled", "info", json.dumps(rec))
    assert rec["status"] == "AUDITED"

    # Internal consistency that must hold whatever the evidence said.
    subs, fee = int(rec["subscribers"]), FEE
    refund = fee * int(rec["refund_bps"]) // 10_000
    penalty = fee * int(rec["penalty_bps"]) // 10_000
    assert int(rec["comp_per_subscriber"]) == refund + penalty
    assert int(rec["comp_pool"]) == subs * (refund + penalty)
    gross = subs * (fee - refund)
    assert int(rec["provider_paid"]) == gross - gross * PROTOCOL_FEE_BPS // 10_000

    if EVIDENCE == "mock":   # scripted: 10 minutes down in a 60-minute epoch -> tier 3
        assert rec["tier"] == "3" and rec["downtime_minutes"] == "10"
        assert rec["comp_per_subscriber"] == "7500" and rec["provider_paid"] == "9900"
    CTX["settled"] = rec


def test_09_claims_pay_out_exactly_once(accounts):
    _need("settled")
    rec = CTX["settled"]
    per_sub = int(rec["comp_per_subscriber"])
    alice = CTX["alice_sub"]
    claimable = dict(_view("get_claimable", alice))
    step("alice's claimable amount", "info", json.dumps(claimable))
    if per_sub == 0:
        step("no breach: nothing to claim", "ok", "provider met the target")
        assert claimable["total"] == "0"
        assert _is_rejected("claim with nothing owed", "claim_all_compensation", [alice], accounts[1])
        return
    assert claimable["total"] == str(per_sub)
    _succeeds("alice claims her compensation", "claim_all_compensation", [alice], accounts[1])
    assert dict(_view("get_claimable", alice))["total"] == "0"
    assert _is_rejected("alice claims a second time", "claim_all_compensation", [alice], accounts[1])
    _succeeds("bob claims his compensation", "claim_all_compensation", [CTX["bob_sub"]], accounts[2])


def test_10_final_books_balance(gl_client):
    _need("settled")
    stats = _stats()
    step("final get_stats", "info", json.dumps(stats))
    assert int(stats["total_epochs_audited"]) == 1
    expected = (int(stats["total_bond_held"]) + int(stats["total_escrowed_fees"])
                + int(stats["total_compensation_pending"]))
    assert int(stats["contract_balance_expected"]) == expected
    actual = _balance(gl_client, CTX["address"])
    if actual is not None:
        step("final on-chain balance", "ok" if actual == expected else "fail", f"on-chain {actual}, books {expected}")
        assert actual == expected
