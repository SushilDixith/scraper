import argparse
import logging
import os
import random
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
)
from playwright_stealth import Stealth

from config import CONFIG, GENERIC_SEARCH_SELECTORS

logger = logging.getLogger("site_scraper")


def setup_logging(level: str, log_file: Path):
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    try:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as e:
        # If we can't write a log file, keep going with console-only logging.
        logger.warning(f"Could not open log file {log_file}: {e}. Logging to console only.")


def slugify(text: str) -> str:
    """Turn text into a safe filename fragment."""
    keep = [c if c.isalnum() else "_" for c in text.strip().lower()]
    slug = "".join(keep)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "page"


def jitter_ms(page, bounds):
    """Wait a random amount of time within a (min_ms, max_ms) range."""
    low, high = bounds
    page.wait_for_timeout(random.uniform(low, high))


def human_mouse_move_and_click(page, locator):
    """Move the mouse toward an element in a few steps, then click, instead of
    teleporting the cursor straight onto the target."""
    box = locator.bounding_box()
    if box is None:
        locator.click()
        return
    target_x = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
    target_y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)

    # Start from a random nearby point and move in a couple of intermediate
    # steps so the cursor doesn't snap directly onto the target.
    start_x = target_x + random.uniform(-150, 150)
    start_y = target_y + random.uniform(-150, 150)
    page.mouse.move(start_x, start_y)

    steps = random.randint(3, 6)
    for i in range(1, steps + 1):
        frac = i / steps
        x = start_x + (target_x - start_x) * frac + random.uniform(-4, 4)
        y = start_y + (target_y - start_y) * frac + random.uniform(-4, 4)
        page.mouse.move(x, y)
        page.wait_for_timeout(random.uniform(10, 40))

    page.mouse.click(target_x, target_y)


def human_type(page, locator, text, typing_delay_bounds):
    """Type character by character with randomized per-keystroke delay,
    instead of filling the whole string in instantly."""
    low, high = typing_delay_bounds
    for char in text:
        locator.press_sequentially(char, delay=0)
        page.wait_for_timeout(random.uniform(low, high))
        # Occasionally pause a bit longer, like someone reading what they typed.
        if random.random() < 0.08:
            page.wait_for_timeout(random.uniform(high, high * 2.5))


def human_scroll(page, pause_bounds, step_bounds, max_scrolls: int, back_probability: float):
    """Scroll incrementally with randomized step size and pauses, with an
    occasional small upward correction, until page height stops changing."""
    try:
        last_height = page.evaluate("document.body.scrollHeight")
        for i in range(max_scrolls):
            step = random.uniform(*step_bounds)
            page.mouse.wheel(0, step)
            jitter_ms(page, pause_bounds)

            if random.random() < back_probability:
                page.mouse.wheel(0, -random.uniform(50, 200))
                page.wait_for_timeout(random.uniform(100, 300))

            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                logger.debug(f"Reached bottom after {i + 1} scroll step(s).")
                break
            last_height = new_height
        else:
            logger.warning(f"Hit max_scrolls limit ({max_scrolls}) before page height stabilized.")
    except PlaywrightError as e:
        logger.warning(f"Scrolling stopped early due to a page error: {e}")


def find_search_box(page, explicit_selector, timeout_ms: int):
    """Locate a usable search input on the page."""
    candidates = [explicit_selector] if explicit_selector else []
    candidates += GENERIC_SEARCH_SELECTORS

    for selector in candidates:
        if not selector:
            continue
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            logger.info(f"Search box found with selector: {selector}")
            return locator
        except PlaywrightTimeoutError:
            continue
    return None


def asset_path_for_url(url: str, assets_dir: Path) -> Path:
    """Map a response URL to a local file path under assets_dir, mirroring
    the site's own host/path structure so the folder stays organized."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path.endswith("/"):
        path += "index.html"
    # Keep query strings out of the filesystem path but keep them
    # distinguishable, since the same path with different query params
    # (e.g. resized images, cache-busted JS) is common.
    if parsed.query:
        safe_query = slugify(parsed.query)[:80]
        path += f"__{safe_query}"
    rel = Path(parsed.netloc or "unknown-host") / path.lstrip("/")
    return assets_dir / rel


def make_asset_saver(assets_dir: Path):
    """Return a Playwright 'response' event handler that mirrors every
    response body (CSS, JS, images, fonts, XHR, etc.) to disk."""

    def on_response(response):
        try:
            url = response.url
            if not url.startswith("http"):
                return  # skip data:, blob:, chrome-extension:, etc.
            if response.status >= 300:
                return  # skip redirects/errors, nothing useful to save
            body = response.body()
            if not body:
                return
            file_path = asset_path_for_url(url, assets_dir)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(body)
        except Exception as e:
            # Assets are best-effort: a single failed capture (aborted
            # request, streamed response, etc.) shouldn't stop the run.
            logger.debug(f"Could not save asset {getattr(response, 'url', '?')}: {e}")

    return on_response


# Header/cookie/body signatures used to flag CDN or bot-mitigation
# redirection. These are best-effort heuristics, not proof — some sites
# front Cloudflare/Akamai without ever showing a challenge page.
CLOUDFLARE_HEADER_HINTS = ("cf-ray", "cf-cache-status", "cf-mitigated", "cf-chl-bypass")
CLOUDFLARE_COOKIE_HINTS = ("__cfduid", "cf_clearance", "__cf_bm")
CLOUDFLARE_BODY_HINTS = ("checking your browser", "just a moment", "attention required! | cloudflare", "cf-chl")

AKAMAI_HEADER_HINTS = ("akamai-", "x-akamai-", "ak_bmsc")
AKAMAI_COOKIE_HINTS = ("ak_bmsc", "bm_sv", "bm_sz", "abck", "_abck")
AKAMAI_BODY_HINTS = ("reference #", "access denied", "akamai")


def make_cdn_detector(state: dict):
    """Return a Playwright 'response' handler that inspects headers (not
    bodies) on every response for Cloudflare/Akamai signatures. Cheap
    enough to run always, independent of asset saving."""

    def on_response(response):
        try:
            headers = {k.lower(): v for k, v in response.headers.items()}
            server = headers.get("server", "").lower()

            if "cloudflare" in server or any(h in headers for h in CLOUDFLARE_HEADER_HINTS):
                state["cloudflare_urls"].add(response.url)
            if "akamaighost" in server or any(
                h.startswith(("akamai-", "x-akamai-")) for h in headers
            ):
                state["akamai_urls"].add(response.url)
        except Exception as e:
            logger.debug(f"CDN detection skipped for a response: {e}")

    return on_response


def detect_cdn_from_cookies_and_body(page, state: dict):
    """Supplement header-based detection with cookies and a body-text scan
    for common challenge/block pages."""
    try:
        cookie_names = {c["name"].lower() for c in page.context.cookies()}
        if any(name in cookie_names for name in CLOUDFLARE_COOKIE_HINTS):
            state["cloudflare_urls"].add("(cookie evidence)")
        if any(name in cookie_names for name in AKAMAI_COOKIE_HINTS):
            state["akamai_urls"].add("(cookie evidence)")
    except Exception as e:
        logger.debug(f"Cookie-based CDN detection failed: {e}")

    try:
        body_text = page.content().lower()
        if any(hint in body_text for hint in CLOUDFLARE_BODY_HINTS):
            state["cloudflare_urls"].add("(page-body evidence)")
        if any(hint in body_text for hint in AKAMAI_BODY_HINTS):
            state["akamai_urls"].add("(page-body evidence)")
    except Exception as e:
        logger.debug(f"Body-text CDN detection failed: {e}")


def write_cdn_report(report_path: Path, requested_url: str, final_url: str, state: dict):
    """Write a plain-text summary noting whether Cloudflare or Akamai
    redirection/protection was observed during the run."""
    cf_hits = sorted(state["cloudflare_urls"])
    ak_hits = sorted(state["akamai_urls"])

    lines = [
        f"Requested URL: {requested_url}",
        f"Final URL:     {final_url}",
        f"Redirected:    {'yes' if requested_url.rstrip('/') != final_url.rstrip('/') else 'no'}",
        "",
        f"Cloudflare detected: {'yes' if cf_hits else 'no'}",
    ]
    if cf_hits:
        lines.append("  Evidence:")
        lines += [f"    - {hit}" for hit in cf_hits[:20]]
        if len(cf_hits) > 20:
            lines.append(f"    ... and {len(cf_hits) - 20} more")

    lines.append("")
    lines.append(f"Akamai detected: {'yes' if ak_hits else 'no'}")
    if ak_hits:
        lines.append("  Evidence:")
        lines += [f"    - {hit}" for hit in ak_hits[:20]]
        if len(ak_hits) > 20:
            lines.append(f"    ... and {len(ak_hits) - 20} more")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(url: str, keyword: str, selector: str, cfg: dict) -> int:
    """
    Run the full automation. Returns an exit code:
    0 = success, 1 = failed to load page, 2 = other failure.
    """
    output_dir = Path(cfg["output_dir"])
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = slugify(keyword or url)
    run_dir = output_dir / f"{slug}_{timestamp}"
    assets_dir = run_dir / "assets"

    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        if cfg["save_assets"]:
            assets_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"Could not create output folder {run_dir}: {e}")
        return 2

    screenshot_path = run_dir / "screenshot.png"
    html_path = run_dir / "page.html"
    cdn_report_path = run_dir / "cdn_report.txt"
    cdn_state = {"cloudflare_urls": set(), "akamai_urls": set()}

    try:
        with sync_playwright() as p:
            user_data_dir = tempfile.mkdtemp(prefix="site_scraper_profile_")
            viewport_w, viewport_h = random.choice(cfg["viewport_pool"])
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=cfg["headless"],
                    channel=cfg["channel"],
                    viewport={"width": viewport_w, "height": viewport_h},
                    screen={"width": viewport_w, "height": viewport_h},
                    args=["--disable-blink-features=AutomationControlled"],
                    ignore_default_args=["--enable-automation"],
                )
            except PlaywrightError as e:
                logger.error(
                    f"Could not launch Chrome (channel='{cfg['channel']}'): {e}. "
                    "Make sure Chrome is installed, e.g. run: playwright install chrome"
                )
                shutil.rmtree(user_data_dir, ignore_errors=True)
                return 2

            if hasattr(os, "geteuid") and os.geteuid() == 0:
                logger.warning(
                    "Running as root: Playwright automatically adds --no-sandbox in "
                    "this case (real Chrome won't start as root otherwise), which "
                    "triggers Chrome's 'unsupported command-line flag' infobar and is "
                    "itself a detectable signal. Run this script as a regular, "
                    "non-root user to avoid needing it at all."
                )

            try:
                # launch_persistent_context() always starts with one tab already
                # open (same as a normal Chrome launch). Reuse it instead of
                # calling new_page(), which was leaving a stray extra "about:blank"
                # tab sitting open next to the one we actually navigate.
                page = context.pages[0] if context.pages else context.new_page()
                # The manual navigator.webdriver patch plus the launch flags weren't
                # enough on their own: real detection scripts (like bot.sannysoft.com)
                # cross-check several fingerprints against each other (webdriver flag,
                # chrome.runtime, plugin list, permissions API, webgl vendor string,
                # etc.), so a single spoofed property stands out as inconsistent.
                # playwright-stealth patches this whole set together.
                Stealth().apply_stealth_sync(page)

                if cfg["save_assets"]:
                    page.on("response", make_asset_saver(assets_dir))
                page.on("response", make_cdn_detector(cdn_state))

                logger.info(f"Opening {url} ...")
                try:
                    page.goto(url, wait_until="load", timeout=cfg["nav_timeout_ms"])
                except PlaywrightTimeoutError:
                    logger.error(f"Timed out loading {url} (limit: {cfg['nav_timeout_ms']}ms). Check the URL and your connection.")
                    return 1
                except PlaywrightError as e:
                    logger.error(f"Failed to open {url}: {e}")
                    return 1

                # A brief pause after the page loads, like a person visually
                # orienting on a new page before doing anything.
                jitter_ms(page, cfg["post_load_pause_ms"])

                if keyword:
                    search_box = find_search_box(page, selector, cfg["search_box_timeout_ms"])
                    if search_box is None:
                        logger.warning(
                            "Could not find a search box automatically. "
                            "Pass --selector '<css selector>' to point at it directly. "
                            "Continuing without searching."
                        )
                    else:
                        try:
                            jitter_ms(page, cfg["pre_search_pause_ms"])
                            human_mouse_move_and_click(page, search_box)
                            human_type(page, search_box, keyword, cfg["typing_delay_ms"])
                            page.wait_for_timeout(random.uniform(150, 500))
                            search_box.press("Enter")
                            try:
                                page.wait_for_load_state("networkidle", timeout=cfg["network_idle_timeout_ms"])
                            except PlaywrightTimeoutError:
                                logger.debug("Network didn't fully settle after search; continuing anyway.")
                            logger.info(f"Searched for: {keyword}")
                            logger.info(f"Landed on: {page.url}")
                        except PlaywrightError as e:
                            logger.warning(f"Search interaction failed, continuing without it: {e}")

                logger.info("Scrolling to the bottom of the page...")
                human_scroll(
                    page,
                    cfg["scroll_pause_ms"],
                    cfg["scroll_step_px"],
                    cfg["max_scrolls"],
                    cfg["scroll_back_probability"],
                )

                try:
                    logger.info(f"Saving screenshot to {screenshot_path}")
                    page.screenshot(path=str(screenshot_path), full_page=True)
                except PlaywrightError as e:
                    logger.error(f"Failed to capture screenshot: {e}")

                try:
                    logger.info(f"Saving HTML to {html_path}")
                    html_path.write_text(page.content(), encoding="utf-8")
                except (PlaywrightError, OSError) as e:
                    logger.error(f"Failed to save HTML: {e}")

                detect_cdn_from_cookies_and_body(page, cdn_state)
                try:
                    logger.info(f"Saving CDN report to {cdn_report_path}")
                    write_cdn_report(cdn_report_path, url, page.url, cdn_state)
                except OSError as e:
                    logger.error(f"Failed to save CDN report: {e}")

                if cfg["save_assets"]:
                    # Give any still-in-flight responses a moment to land
                    # before we stop capturing.
                    page.wait_for_timeout(500)
            finally:
                context.close()
                shutil.rmtree(user_data_dir, ignore_errors=True)

    except PlaywrightError as e:
        logger.error(f"Unexpected Playwright error: {e}")
        return 2
    except Exception as e:  # last-resort safety net so the script never crashes with a raw traceback
        logger.error(f"Unexpected error: {e}")
        return 2

    logger.info("Done.")
    logger.info(f"  Run folder: {run_dir}")
    logger.info(f"  Screenshot: {screenshot_path}")
    logger.info(f"  HTML file:  {html_path}")
    logger.info(f"  CDN report: {cdn_report_path}")
    if cfg["save_assets"]:
        logger.info(f"  Assets:     {assets_dir}")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description="Search a website, scroll it, and capture a full page mirror (HTML, CSS, images, screenshot).")
    parser.add_argument("--url", help="Full URL of the website to open.")
    parser.add_argument("--keyword", help="Keyword to type into the search bar.")
    parser.add_argument("--selector", help="CSS selector for the search input (for unlisted sites).")
    parser.add_argument("--output-dir", help="Folder to save each run's subfolder into.")
    parser.add_argument("--save-assets", action="store_true", help="Also mirror every CSS/JS/image/font/XHR response to disk. Off by default — only HTML, screenshot, and the CDN report are saved.")

    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument("--headless", action="store_true", default=None, help="Run Chrome headless (no visible window). This is the default.")
    headless_group.add_argument("--headed", action="store_true", help="Show the Chrome window instead of running headless.")

    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity.")
    return parser.parse_args()


def main():
    args = parse_args()
    cfg = dict(CONFIG)  # start from config.py defaults, then apply overrides

    if args.output_dir:
        cfg["output_dir"] = Path(args.output_dir)
    if args.headed:
        cfg["headless"] = False
    elif args.headless:
        cfg["headless"] = True
    if args.save_assets:
        cfg["save_assets"] = True
    if args.log_level:
        cfg["log_level"] = args.log_level

    setup_logging(cfg["log_level"], Path(cfg["log_file"]))

    url = args.url or cfg["url"]
    selector = args.selector or cfg["selector"]

    if not url:
        url = input("Enter a full URL: ").strip()

    if not url:
        logger.error("No URL provided. Exiting.")
        sys.exit(1)

    if not url.startswith("http"):
        url = "https://" + url

    keyword = args.keyword if args.keyword is not None else cfg["keyword"]
    if keyword is None:
        keyword = input("Enter a keyword to search (leave blank to skip searching): ").strip()

    exit_code = run(url=url, keyword=keyword, selector=selector, cfg=cfg)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
