"""Live test report for pytest / gltest runs (loaded from the root conftest.py).

After EVERY test it rewrites three files in the report directory:

    report.html   self-contained page that refreshes itself while the run is in progress
    report.md     the same content as Markdown (also appended to $GITHUB_STEP_SUMMARY in CI)
    report.json   machine-readable state

and keeps <reports root>/index.html, a landing page listing every suite's report.
Tests can add narrative steps (transaction hashes, on-chain values) with `step(...)`.

Configuration is by environment variable, so it also works through the `gltest` CLI:

    LIVE_REPORT_DIR    where to write (default: reports/tests)
    LIVE_REPORT_TITLE  heading of the report
"""

import datetime as dt
import html
import json
import os
import pathlib
import platform
import sys
import threading
import time

_LOCK = threading.RLock()
_S = {}                       # the run state; empty until pytest_configure
_NOISE_MARKS = {"parametrize", "usefixtures", "filterwarnings", "timeout", "skipif", "skip", "xfail"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stamp(t=None) -> str:
    return (t or _now()).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# public API for tests
# ---------------------------------------------------------------------------


def step(name: str, status: str = "ok", detail: str = "", tx: str = "") -> None:
    """Record one narrative step of the current test.

    status is one of: ok, fail, info, warn, skip.
    """
    with _LOCK:
        if not _S:
            return
        _S["steps"].append(
            {
                "time": _stamp(),
                "test": _S.get("current", ""),
                "name": str(name),
                "status": status if status in ("ok", "fail", "info", "warn", "skip") else "info",
                "detail": str(detail)[:2000],
                "tx": str(tx or ""),
            }
        )
        _write(force=True)


# ---------------------------------------------------------------------------
# state and rendering
# ---------------------------------------------------------------------------


def _counts() -> dict:
    out = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "pending": 0}
    for t in _S["tests"].values():
        out[t["outcome"]] = out.get(t["outcome"], 0) + 1
    return out


def _groups() -> list:
    groups = {}
    for t in _S["tests"].values():
        for g in (t["markers"] or ["unmarked"]):
            row = groups.setdefault(g, {"group": g, "total": 0, "passed": 0, "failed": 0, "other": 0})
            row["total"] += 1
            if t["outcome"] == "passed":
                row["passed"] += 1
            elif t["outcome"] in ("failed", "error"):
                row["failed"] += 1
            else:
                row["other"] += 1
    return sorted(groups.values(), key=lambda r: r["group"])


def _snapshot() -> dict:
    counts = _counts()
    return {
        "title": _S["title"],
        "status": _S["status"],
        "started_at": _stamp(_S["started"]),
        "updated_at": _stamp(),
        "finished_at": _stamp(_S["finished"]) if _S.get("finished") else None,
        "elapsed_seconds": round(((_S.get("finished") or _now()) - _S["started"]).total_seconds(), 1),
        "environment": _S["environment"],
        "current": _S.get("current", ""),
        "total": len(_S["tests"]),
        "counts": counts,
        "groups": _groups(),
        "tests": list(_S["tests"].values()),
        "steps": _S["steps"],
    }


_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1b1f24;--mut:#656d76;--line:#d0d7de;--card:#f6f8fa;
--ok:#1a7f37;--fail:#cf222e;--warn:#9a6700;--info:#0969da;--skip:#6e7781}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--line:#30363d;--card:#161b22;
--ok:#3fb950;--fail:#f85149;--warn:#d29922;--info:#58a6ff;--skip:#8b949e}}
*{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.mut{color:var(--mut)}.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px;
border:1px solid currentColor}.running{color:var(--info)}.finished.good{color:var(--ok)}.finished.bad{color:var(--fail)}
.bar{height:10px;border-radius:5px;background:var(--card);border:1px solid var(--line);overflow:hidden;margin:12px 0}
.bar i{display:block;height:100%;float:left}.cards{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 14px;min-width:92px}
.card b{display:block;font-size:20px}table{border-collapse:collapse;width:100%;margin:6px 0}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
td.n{text-align:right;font-variant-numeric:tabular-nums}code,pre{font:12px ui-monospace,Menlo,Consolas,monospace}
pre{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto;max-height:340px;white-space:pre-wrap}
.passed,.ok{color:var(--ok)}.failed,.error,.fail{color:var(--fail)}.skipped,.skip,.pending{color:var(--skip)}
.warn,.xfailed,.xpassed{color:var(--warn)}.info{color:var(--info)}.wrap{overflow-x:auto}
a{color:var(--info)}details summary{cursor:pointer}
"""

_COLORS = {"passed": "var(--ok)", "failed": "var(--fail)", "error": "var(--fail)",
           "skipped": "var(--skip)", "xfailed": "var(--warn)", "xpassed": "var(--warn)", "pending": "var(--line)"}


def _e(x) -> str:
    return html.escape(str(x))


def _render_html(snap: dict) -> str:
    running = snap["status"] == "running"
    counts, total = snap["counts"], max(1, snap["total"])
    bad = counts["failed"] + counts["error"]
    state_cls = "running" if running else ("finished bad" if bad else "finished good")
    state_txt = "RUNNING" if running else ("FAILED" if bad else "PASSED")
    bar = "".join(
        f'<i style="width:{100.0 * counts[k] / total:.2f}%;background:{_COLORS[k]}"></i>'
        for k in ("passed", "failed", "error", "xfailed", "xpassed", "skipped", "pending") if counts[k]
    )
    done = snap["total"] - counts["pending"]
    cards = "".join(
        f'<div class="card"><span class="mut">{k}</span><b class="{k}">{counts[k]}</b></div>'
        for k in ("passed", "failed", "error", "skipped", "pending") if counts[k] or k in ("passed", "failed")
    )
    env = " · ".join(f"{_e(k)}: {_e(v)}" for k, v in snap["environment"].items() if v)

    groups = "".join(
        f'<tr><td>{_e(g["group"])}</td><td class="n">{g["total"]}</td><td class="n passed">{g["passed"]}</td>'
        f'<td class="n failed">{g["failed"]}</td><td class="n">{g["other"]}</td></tr>' for g in snap["groups"]
    )
    order = {"failed": 0, "error": 0, "xpassed": 1, "pending": 3, "passed": 4, "skipped": 4, "xfailed": 4}
    tests = sorted(snap["tests"], key=lambda t: (order.get(t["outcome"], 5),))
    rows = []
    for t in tests:
        detail = f'<details><summary>details</summary><pre>{_e(t["detail"])}</pre></details>' if t["detail"] else ""
        marks = " ".join(f'<span class="mut">@{_e(m)}</span>' for m in t["markers"])
        current = " ◀ running" if running and t["nodeid"] == snap["current"] else ""
        rows.append(
            f'<tr><td class="{t["outcome"]}"><b>{_e(t["outcome"].upper())}</b></td>'
            f'<td><code>{_e(t["nodeid"])}</code>{_e(current)} {marks}{detail}</td>'
            f'<td class="n">{t["duration"]:.2f}s</td></tr>'
        )
    steps = ""
    if snap["steps"]:
        srows = "".join(
            f'<tr><td class="mut">{_e(s["time"][11:19])}</td><td class="{s["status"]}"><b>{_e(s["status"].upper())}</b></td>'
            f'<td><b>{_e(s["name"])}</b><br><span class="mut">{_e(s["detail"])}</span></td>'
            f'<td><code>{_e(s["tx"])}</code></td></tr>' for s in snap["steps"]
        )
        steps = (f'<h2>Step-by-step log</h2><div class="wrap"><table><tr><th>Time</th><th>Result</th>'
                 f'<th>Step</th><th>Transaction</th></tr>{srows}</table></div>')
    refresh = '<meta http-equiv="refresh" content="3">' if running else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">{refresh}
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(snap["title"])}</title>
<style>{_CSS}</style></head><body>
<p class="mut"><a href="../index.html">← all reports</a></p>
<h1>{_e(snap["title"])} <span class="pill {state_cls}">{state_txt}</span></h1>
<div class="mut">started {_e(snap["started_at"])} · updated {_e(snap["updated_at"])} · {snap["elapsed_seconds"]}s elapsed
· {done}/{snap["total"]} finished</div><div class="mut">{env}</div>
<div class="bar">{bar}</div><div class="cards">{cards}</div>
<h2>By group</h2><table><tr><th>Group</th><th>Tests</th><th>Passed</th><th>Failed</th><th>Other</th></tr>{groups}</table>
{steps}
<h2>Tests</h2><div class="wrap"><table><tr><th>Result</th><th>Test</th><th>Time</th></tr>{"".join(rows)}</table></div>
</body></html>"""


def _render_md(snap: dict) -> str:
    c = snap["counts"]
    verdict = "RUNNING" if snap["status"] == "running" else ("FAILED" if c["failed"] + c["error"] else "PASSED")
    lines = [f"## {snap['title']} — {verdict}", "",
             f"{snap['total'] - c['pending']}/{snap['total']} finished in {snap['elapsed_seconds']}s · "
             f"✅ {c['passed']} passed · ❌ {c['failed'] + c['error']} failed · ⏭ {c['skipped']} skipped", "",
             "| Group | Tests | Passed | Failed | Other |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {g['group']} | {g['total']} | {g['passed']} | {g['failed']} | {g['other']} |" for g in snap["groups"]]
    bad = [t for t in snap["tests"] if t["outcome"] in ("failed", "error")]
    if bad:
        lines += ["", "### Failures", ""]
        for t in bad:
            first = (t["detail"].strip().splitlines() or [""])[-1][:300]
            lines.append(f"- `{t['nodeid']}` — {first}")
    if snap["steps"]:
        lines += ["", "### Step-by-step log", "", "| Time | Result | Step | Transaction |", "|---|---|---|---|"]
        for s in snap["steps"]:
            detail = (s["detail"] or "").replace("|", "\\|").replace("\n", " ")[:200]
            lines.append(f"| {s['time'][11:19]} | {s['status']} | **{s['name']}** {detail} | `{s['tx']}` |")
    return "\n".join(lines) + "\n"


def _write_index(root: pathlib.Path) -> None:
    items, running = [], False
    for p in sorted(root.glob("*/report.json")):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        c = d["counts"]
        bad = c["failed"] + c["error"]
        running = running or d["status"] == "running"
        state = "running" if d["status"] == "running" else ("failed" if bad else "passed")
        items.append(
            f'<tr><td><a href="{_e(p.parent.name)}/report.html">{_e(d["title"])}</a></td>'
            f'<td class="{"failed" if state == "failed" else "passed" if state == "passed" else "info"}"><b>{state.upper()}</b></td>'
            f'<td class="n">{c["passed"]}</td><td class="n failed">{bad}</td><td class="n">{c["skipped"]}</td>'
            f'<td class="n">{d["total"] - c["pending"]}/{d["total"]}</td><td class="mut">{_e(d["updated_at"])}</td></tr>'
        )
    refresh = '<meta http-equiv="refresh" content="3">' if running else ""
    (root / "index.html").write_text(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">{refresh}<title>Test reports</title>'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{_CSS}</style></head><body>'
        f'<h1>Test reports</h1><div class="wrap"><table><tr><th>Suite</th><th>Result</th><th>Passed</th>'
        f'<th>Failed</th><th>Skipped</th><th>Progress</th><th>Updated</th></tr>{"".join(items)}</table></div>'
        f'</body></html>'
    )


def _write(force: bool = False) -> None:
    with _LOCK:
        if not _S:
            return
        if not force and time.monotonic() - _S["last_write"] < 0.4:
            return
        _S["last_write"] = time.monotonic()
        snap = _snapshot()
        out = _S["dir"]
        try:
            out.mkdir(parents=True, exist_ok=True)
            for name, text in (("report.json", json.dumps(snap, indent=1)),
                               ("report.md", _render_md(snap)),
                               ("report.html", _render_html(snap))):
                tmp = out / (name + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(out / name)          # readers never see a half-written file
            _write_index(out.parent)
        except OSError as exc:                   # a report problem must never fail the test run
            print(f"[live_report] could not write report: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# pytest hooks
# ---------------------------------------------------------------------------


def pytest_configure(config):
    root = pathlib.Path(str(getattr(config, "rootpath", pathlib.Path.cwd())))
    out = pathlib.Path(os.environ.get("LIVE_REPORT_DIR") or root / "reports" / "tests")
    if not out.is_absolute():
        out = root / out
    with _LOCK:
        _S.clear()
        _S.update(
            title=os.environ.get("LIVE_REPORT_TITLE", "Test run"),
            dir=out, status="running", started=_now(), finished=None, current="", last_write=0.0,
            tests={}, steps=[],
            environment={
                "python": platform.python_version(),
                "platform": platform.system(),
                "network": os.environ.get("E2E_NETWORK", ""),
                "ci": "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else (
                    "Codespaces" if os.environ.get("CODESPACES") else ""),
            },
        )
        _write(force=True)


def pytest_collection_finish(session):
    with _LOCK:
        for item in session.items:
            marks = sorted({m.name for m in item.iter_markers()} - _NOISE_MARKS)
            _S["tests"][item.nodeid] = {"nodeid": item.nodeid, "outcome": "pending", "duration": 0.0,
                                        "markers": marks, "detail": ""}
        _write(force=True)


def pytest_runtest_logstart(nodeid, location):
    with _LOCK:
        _S["current"] = nodeid
        _write()


def _reason(report) -> str:
    lr = getattr(report, "longrepr", None)
    if isinstance(lr, tuple) and len(lr) == 3:
        return str(lr[2])
    return getattr(report, "longreprtext", "") or str(lr or "")


def pytest_runtest_logreport(report):
    with _LOCK:
        if not _S:
            return
        t = _S["tests"].setdefault(report.nodeid, {"nodeid": report.nodeid, "outcome": "pending",
                                                   "duration": 0.0, "markers": [], "detail": ""})
        t["duration"] += float(getattr(report, "duration", 0.0) or 0.0)
        if report.when == "call":
            if hasattr(report, "wasxfail"):
                t["outcome"] = "xpassed" if report.outcome == "passed" else "xfailed"
            else:
                t["outcome"] = report.outcome
            if report.outcome == "failed":
                t["detail"] = (getattr(report, "longreprtext", "") or str(report.longrepr))[-6000:]
            elif report.outcome == "skipped":
                t["detail"] = _reason(report)[-1000:]
        elif report.when == "setup" and report.outcome in ("failed", "skipped"):
            t["outcome"] = "error" if report.outcome == "failed" else "skipped"
            t["detail"] = (getattr(report, "longreprtext", "") or _reason(report))[-6000:]
        elif report.when == "teardown" and report.outcome == "failed":
            t["outcome"] = "error"
            t["detail"] = ((t["detail"] + "\n--- teardown ---\n") if t["detail"] else "") + \
                (getattr(report, "longreprtext", "") or str(report.longrepr))[-4000:]
        _write(force=t["outcome"] in ("failed", "error") or report.when == "teardown")


def pytest_sessionfinish(session, exitstatus):
    with _LOCK:
        if not _S:
            return
        _S["status"], _S["finished"], _S["current"] = "finished", _now(), ""
        _write(force=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            try:
                with open(summary, "a", encoding="utf-8") as fh:
                    fh.write(_render_md(_snapshot()) + "\n")
            except OSError:
                pass


def pytest_terminal_summary(terminalreporter):
    if _S:
        terminalreporter.write_line(f"live report: {_S['dir'] / 'report.html'}")
