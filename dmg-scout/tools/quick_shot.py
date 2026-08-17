"""Fast single-purpose screenshot helper for the 2026-08-19 bugfix pass --
NOT the full before/after verification tool (that's screenshot_visual_pass.py).
Seeds once, boots uvicorn once, shoots whatever routes/viewport you ask for.

    .venv/bin/python tools/quick_shot.py step1 board today --width 1440 --theme dark
"""
import argparse
import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("svp", ROOT / "tools" / "screenshot_visual_pass.py")
svp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(svp)

ROUTES = {"board": "/board", "today": "/", "retrofit": "/retrofit", "contractors": "/contractors"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage")
    ap.add_argument("views", nargs="+", choices=list(ROUTES))
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--theme", default="dark")
    args = ap.parse_args()

    db_path = Path(tempfile.mkdtemp()) / "quick.db"
    svp.seed(db_path)
    port = svp.free_port()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "DASHBOARD_PASSWORD": "x"}
    proc = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python3"), "-m", "uvicorn", "app.web.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env,
    )
    out_dir = ROOT / "tools" / "screenshots" / "bugfix"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        svp.wait_ready(port, timeout=60)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(http_credentials={"username": "andrew", "password": "x"})
            page = ctx.new_page()
            page.set_viewport_size({"width": args.width, "height": 1000})
            for view in args.views:
                page.goto(f"http://127.0.0.1:{port}{ROUTES[view]}", wait_until="networkidle")
                page.evaluate(f"document.documentElement.dataset.theme = '{args.theme}'")
                page.wait_for_timeout(200)
                out = out_dir / f"{view}-{args.stage}-{args.width}-{args.theme}.png"
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
