"""Before/after screenshot capture for the 2026-08-17 visual pass (see
/home/andrewcrane/.claude/plans/parallel-crunching-clover.md). Not a pytest
test -- a one-off verification script, run manually at each checkpoint
(before any change / after commit 1 / after commit 2):

    .venv/bin/python tools/screenshot_visual_pass.py before
    .venv/bin/python tools/screenshot_visual_pass.py after1
    .venv/bin/python tools/screenshot_visual_pass.py after2

Seeds a throwaway sqlite DB with a few representative rows (reusing
app.manual.add_manual_signal + run_resolve + run_size_score, the same seed
path tests/test_web.py uses, plus a handful of RetrofitBuilding rows built
directly), boots a real uvicorn server against it, and screenshots Board,
Retrofit, and Today at 390px/1440px in both themes to
tools/screenshots/<stage>/<view>-<width>-<theme>.png.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed(db_path: Path) -> None:
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    # only knows about classes that have been imported at least once, and
    # create_all() against an empty registry silently creates zero tables.
    import app.db as db_mod
    import app.models  # noqa: F401 -- import BEFORE init_db(): SQLModel.metadata
    db_mod._engine = None
    db_mod.init_db()

    from datetime import timedelta

    from app.manual import add_manual_signal
    from app.models import RetrofitBuilding, Signal, utcnow
    from app.pipeline.resolve import run_resolve
    from app.pipeline.size_score import run_size_score

    with db_mod.session_scope() as s:
        sig1 = add_manual_signal(s, "prequal_invite", "ACCO invited prequal for Meridian",
                                 project_name="Meridian DC", developer="Vantage Data Centers",
                                 county="San Bernardino", state="CA", mw_it=176, stage="design")
        db_row = s.get(Signal, sig1.id)
        db_row.named_people = [{"name": "Jane Doe", "title": "Mechanical Engineer",
                                "org": "Vantage Data Centers", "phone": "555-0100"}]
        s.add(db_row)
        s.commit()

    with db_mod.session_scope() as s:
        add_manual_signal(s, "ceqa_nop", "NOP filed for Ontario Ranch Campus",
                          project_name="Ontario Ranch Campus", developer="QTS Realty",
                          county="San Bernardino", state="CA", mw_it=90, stage="entitlement")
        add_manual_signal(s, "land_transfer", "Grading permit for Norco facility",
                          project_name="Norco Industrial", developer="Prologis",
                          county="Riverside", state="CA", mw_it=40, stage="construction")

    from app.config import load_config
    cfg = load_config()
    with db_mod.session_scope() as s:
        run_resolve(s, cfg, use_llm=False)
    with db_mod.session_scope() as s:
        run_size_score(s, cfg)

    now = utcnow()
    with db_mod.session_scope() as s:
        s.add(RetrofitBuilding(
            apn="8265004120", population="replacement_candidate",
            address="1600 S AZUSA AVE", county="Los Angeles", state="CA",
            sqft=71000, year_built=1974, use_desc="Warehouse - Distribution",
            service_life_status="overdue", service_life_years_past=22.4,
            equipment_age_years=52, service_life_basis="YearBuilt-derived, 30yr band.",
            rank_score=8.61, last_sale_date=now - timedelta(days=180),
            estimated_tons_low=142, estimated_tons_high=213,
            latitude=34.015, longitude=-117.921,
        ))
        s.add(RetrofitBuilding(
            apn="522804", population="replacement_candidate",
            address="1620 S AZUSA AVE", county="Los Angeles", state="CA",
            sqft=157767, year_built=1998, use_desc="Warehouse - Distribution",
            service_life_status="due", service_life_years_past=1.2,
            equipment_age_years=28, service_life_basis="YearBuilt-derived, 27yr band.",
            rank_score=6.14, last_sale_date=now - timedelta(days=180),
            estimated_tons_low=315, estimated_tons_high=473,
            latitude=34.015, longitude=-117.921,
        ))
        s.add(RetrofitBuilding(
            apn="1010101010", population="replacement_candidate",
            address="4400 RIVERSIDE DR", county="Riverside", state="CA",
            sqft=34500, year_built=2011, use_desc="Light Industrial",
            service_life_status="approaching", service_life_years_past=-3.1,
            equipment_age_years=15, service_life_basis="YearBuilt-derived, 18yr band.",
            rank_score=3.02, sb1206_trigger_status="pending", carb_candidate=True,
            estimated_tons_low=69, estimated_tons_high=103,
        ))
        s.commit()


def wait_ready(port: int, timeout=20) -> None:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("server did not come up")


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "before"
    out_dir = ROOT / "tools" / "screenshots" / stage
    out_dir.mkdir(parents=True, exist_ok=True)

    import tempfile
    db_path = Path(tempfile.mkdtemp()) / "visual_pass.db"
    seed(db_path)

    port = free_port()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}",
          "DASHBOARD_PASSWORD": "visualpass"}
    proc = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python3"), "-m", "uvicorn", "app.web.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env,
    )
    try:
        wait_ready(port, timeout=90)
        from playwright.sync_api import sync_playwright
        views = [("board", "/board"), ("retrofit", "/retrofit"), ("today", "/")]
        widths = [390, 1440]
        themes = ["light", "dark"]
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                http_credentials={"username": "andrew", "password": "visualpass"},
            )
            page = ctx.new_page()
            for width in widths:
                page.set_viewport_size({"width": width, "height": 1000})
                for view, path in views:
                    page.goto(f"http://127.0.0.1:{port}{path}", wait_until="networkidle")
                    for theme in themes:
                        page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
                        page.evaluate("document.fonts.ready")
                        page.wait_for_timeout(250)
                        out = out_dir / f"{view}-{width}-{theme}.png"
                        page.screenshot(path=str(out), full_page=True)
                        print("wrote", out)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
