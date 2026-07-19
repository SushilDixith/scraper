from pathlib import Path

CONFIG = {
    # Default target.
    "url": None,                # e.g. "https://example.com"
    "keyword": None,           # None = don't search unless --keyword is passed (or entered when prompted)
    "selector": None,          # CSS selector override for the search box, if needed

    # Output
    "output_dir": Path(__file__).parent / "output",
    "save_assets": False,      # also mirror every CSS/JS/image/font/XHR response to disk

    # Browser behavior
    "headless": True,          # True = no visible browser window
    "channel": "chrome",       # Playwright browser channel

    # Viewport pool: a randomized-but-realistic size is picked per run instead
    # of always launching with the exact same resolution every time.
    "viewport_pool": [
        (1920, 1080),
        (1366, 768),
        (1536, 864),
        (1440, 900),
        (1280, 720),
    ],

    # Timeouts (milliseconds)
    "nav_timeout_ms": 20000,
    "search_box_timeout_ms": 3000,
    "network_idle_timeout_ms": 15000,

    # Idle/"reading" pauses (randomized ranges, milliseconds)
    "post_load_pause_ms": (400, 1500),   # pause after a page finishes loading, before interacting
    "pre_search_pause_ms": (300, 1200),  # pause after finding the search box, before typing

    # Human-like typing (randomized per-keystroke delay, milliseconds)
    "typing_delay_ms": (60, 220),

    # Scrolling (randomized to avoid a fixed, mechanical scroll pattern)
    "scroll_pause_ms": (250, 900),   # pause between scroll steps
    "scroll_step_px": (400, 1000),   # distance per scroll step
    "scroll_back_probability": 0.15,  # chance of a small upward correction, like overshoot
    "max_scrolls": 200,

    # Logging
    "log_level": "INFO",       # DEBUG, INFO, WARNING, ERROR
    "log_file": Path(__file__).parent / "scraper.log",
}

# Generic fallback selectors tried in order for unknown sites
GENERIC_SEARCH_SELECTORS = [
    "input[type='search']",
    "input[name='q']",
    "input[name='query']",
    "input[name='search']",
    "input[id*='search' i]",
    "input[placeholder*='search' i]",
    "textarea[name='q']",
]
