"""Committee dashboard (Streamlit). Run: uv run streamlit run src/committee/ui/app.py

Pages: Today, Briefing, Portfolio, Scorecards, Journal, Costs, Settings.
Approving here writes the same journaled approval record as the CLI.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import streamlit as st

from committee.broker.approval import ApprovalError, approve, reject
from committee.broker.models import ApprovedLeg
from committee.config.control import pending_versions
from committee.context import AppContext
from committee.core.drift import compute_drift
from committee.core.holdings import HoldingsStore
from committee.journal.anchor import check_anchors
from committee.ui.views import approval_queue, costs, search

st.set_page_config(page_title="Committee", layout="wide")


@st.cache_resource
def ctx() -> AppContext:
    return AppContext.load(
        Path(os.environ["COMMITTEE_ROOT"]) if "COMMITTEE_ROOT" in os.environ else None
    )


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


C = ctx(os.environ.get("COMMITTEE_ROOT"))
page = st.sidebar.radio(
    "Page", ["Today", "Briefing", "Portfolio", "Scorecards", "Journal", "Costs", "Settings"]
)
blocked = C.flags().orders_blocked()
if blocked:
    st.sidebar.error(f"Orders blocked — {blocked}")

with C.journal() as J:
    if page == "Today":
        st.title("Today")
        q = approval_queue(J, now())
        st.subheader(f"Awaiting approval ({len(q)})")
        for item in q:
            status = "ready" if item.ready else f"cooling-off {item.cooling_left}"
            st.write(
                f"**{item.label}** — {item.recommendation} · {status} · expires {item.expires_at:%Y-%m-%d} · `{item.hash[:12]}`"
            )
        inc = [e for e in J.entries("incident") if now() - e.created_at < dt.timedelta(days=7)]
        st.subheader(f"Alerts ({len(inc)} incidents in 7 days)")
        for e in inc[-20:]:
            st.write(
                f"{e.created_at_utc[:16]} {e.payload.get('level')} {e.payload.get('kind')} {e.payload.get('job', '')}"
            )
        dq = J.latest("dq_report")
        st.subheader("Data quality")
        st.write(
            "no DQ report yet"
            if dq is None
            else (dq.payload.get("failures") or "all checks passed")
        )

    elif page == "Briefing":
        st.title("Briefing")
        q = approval_queue(J, now())
        if not q:
            st.info("Nothing awaiting approval.")
        else:
            choice = st.selectbox(
                "Briefing", q, format_func=lambda i: f"{i.label} {i.recommendation} [{i.hash[:10]}]"
            )
            entry = J.find_by_hash(choice.hash)
            assert entry is not None
            st.markdown(
                entry.payload.get("markdown")
                or f"```json\n{json.dumps(entry.payload, indent=2)[:6000]}\n```"
            )
            with st.expander("All agent outputs"):
                for h in (entry.payload.get("agent_output_hashes") or {}).values():
                    ao = J.find_by_hash(h)
                    if ao:
                        st.json(ao.payload)
            if not choice.ready:
                st.warning(
                    f"Cooling-off: {choice.cooling_left} remaining. Approval is disabled until then."
                )
            legs = entry.payload.get("legs", [])
            approved: list[ApprovedLeg] = []
            for i, leg in enumerate(legs):
                pct = st.number_input(
                    f"{leg['side']} {leg['symbol']} in {leg['account']} (% of account, max {leg['max_pct_total']})",
                    min_value=0.0,
                    max_value=float(leg["max_pct_total"]),
                    value=float(leg["max_pct_total"]),
                    key=f"leg{i}",
                )
                if pct > 0:
                    approved.append(
                        ApprovedLeg(
                            symbol=leg["symbol"],
                            side=leg["side"],
                            account=leg["account"],
                            pct_total=pct,
                        )
                    )
            reason = st.text_input("One-sentence reason (required)")
            just = (
                st.text_area("Written justification (required on a Behavioral STOP)")
                if choice.severity == "stop"
                else None
            )
            account_value = st.number_input("Account value (USD)", min_value=1.0, value=100_000.0)
            col1, col2 = st.columns(2)
            if col1.button("Approve", disabled=not choice.ready):
                try:
                    _, e = approve(
                        J, choice.hash, approved, reason, account_value, now(), justification=just
                    )
                    st.success(f"Approved. Place orders with: committee orders place {e.hash}")
                except ApprovalError as err:
                    st.error(str(err))
            if col2.button("Reject"):
                try:
                    reject(J, choice.hash, reason, now())
                    st.success("Rejected (scored with the PASS cohort).")
                except ApprovalError as err:
                    st.error(str(err))

    elif page == "Portfolio":
        st.title("Portfolio")
        store = HoldingsStore(C.state_db)
        positions = store.current()
        if not positions:
            st.info("No holdings yet. Import with: committee core import positions.csv")
        else:
            prices = {p.symbol: (p.cost_per_share or 1.0) for p in positions}
            d = compute_drift(C.config.policy_portfolio, positions, lambda s: prices.get(s, 1.0))
            st.caption(
                "Valued at last imported cost when no price feed is wired into the dashboard."
            )
            st.table(
                [
                    {
                        "sleeve": s.sleeve_id,
                        "target %": s.target_pct,
                        "actual %": round(s.actual_pct, 2),
                        "band": s.band_pct,
                        "breach": s.breached,
                    }
                    for s in d.sleeves
                ]
            )
        sc = J.latest("note")
        st.subheader("Scenario losses")
        st.write("Run `committee scenarios run` for the latest table.")
        del sc

    elif page == "Scorecards":
        st.title("Scorecards")
        st.caption(
            "Short records prove nothing: an information ratio of 0.5 needs about 16 years to reach t = 2."
        )
        weights = J.latest("agent_weights")
        st.write("Agent weights:", weights.payload if weights else "not yet computed")
        res = [e.payload for e in J.entries("forecast_resolution")]
        st.write(f"{len(res)} resolved forecasts.")
        if res:
            st.dataframe(res[-500:])

    elif page == "Journal":
        st.title("Journal")
        rep = J.verify()
        anchors = check_anchors(J, C.path(C.config.app.paths.anchor_dir))
        (st.success if rep.ok and not anchors else st.error)(
            f"verify: {'OK' if rep.ok else rep.first_break} · {rep.entries} entries · head {rep.last_hash[:16]}"
            + (f" · anchor problems: {anchors}" if anchors else "")
        )
        text = st.text_input("Search payloads")
        et = st.selectbox("Entry type", ["(all)", *sorted({e.entry_type for e in J.entries()})])
        rows = search(J, text, None if et == "(all)" else et)
        st.dataframe(
            [
                {
                    "seq": e.seq,
                    "time": e.created_at_utc[:19],
                    "type": e.entry_type,
                    "hash": e.hash[:12],
                    "payload": json.dumps(e.payload)[:200],
                }
                for e in rows
            ]
        )

    elif page == "Costs":
        st.title("Costs")
        c = costs(J, now())
        st.metric(
            "This month (USD)", c["total_usd"], help=f"budget {C.config.models.budget.monthly_usd}"
        )
        st.write(c)

    elif page == "Settings":
        st.title("Settings (read-only)")
        st.subheader("Risk limits (active)")
        st.json(C.active_config(J).risk_limits.model_dump())
        st.subheader("Pending config changes")
        for v in pending_versions(J, now()):
            st.write(
                f"{v.file} effective {v.effective_at:%Y-%m-%d %H:%M} UTC ({v.content_hash[:12]})"
            )
        st.subheader("Kill switch")
        reason = st.text_input("Reason")
        if st.button("ENGAGE KILL SWITCH", type="primary"):
            from committee.broker.factory import make_broker
            from committee.broker.gateway import kill_switch

            n = kill_switch(J, make_broker(C, J), C.flags(), reason or "dashboard kill switch")
            st.error(
                f"Kill switch engaged; {n} open orders cancelled. Release from the CLI after a write-up."
            )
