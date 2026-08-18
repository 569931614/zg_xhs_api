from __future__ import annotations

import logging
import os
import platform
import shlex
import threading
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger("uvicorn.error")

BLOCKED_MARKERS = [
    "just a moment",
    "please wait",
    "请稍候",
    "正在进行安全验证",
    "正在檢查",
    "attention required",
    "access denied",
    "cf-error",
    "sorry, you have been blocked",
]

_cf_semaphore: threading.BoundedSemaphore | None = None
_cf_semaphore_limit = 0
_cf_semaphore_lock = threading.Lock()


class CloudflareBypassError(RuntimeError):
    pass


class CloudflareBypasser:
    def __init__(self, driver: Any, max_retries: int = 8) -> None:
        self.driver = driver
        self.max_retries = max_retries

    def search_recursively_shadow_root_with_iframe(self, element: Any) -> Any | None:
        if element.shadow_root:
            if element.shadow_root.child().tag == "iframe":
                return element.shadow_root.child()
            return None
        for child in element.children():
            result = self.search_recursively_shadow_root_with_iframe(child)
            if result:
                return result
        return None

    def search_recursively_shadow_root_with_cf_input(self, element: Any) -> Any | None:
        if element.shadow_root:
            if element.shadow_root.ele("tag:input"):
                return element.shadow_root.ele("tag:input")
            return None
        for child in element.children():
            result = self.search_recursively_shadow_root_with_cf_input(child)
            if result:
                return result
        return None

    def locate_cf_button(self) -> Any | None:
        button = None
        for element in self.driver.eles("tag:input"):
            attrs = getattr(element, "attrs", {}) or {}
            if "turnstile" in str(attrs.get("name", "")) and attrs.get("type") == "hidden":
                button = element.parent().shadow_root.child()("tag:body").shadow_root("tag:input")
                break
        if button:
            return button

        body = self.driver.ele("tag:body")
        iframe = self.search_recursively_shadow_root_with_iframe(body) if body else None
        if iframe:
            return self.search_recursively_shadow_root_with_cf_input(iframe("tag:body"))
        return None

    def click_verification_button(self) -> None:
        try:
            button = self.locate_cf_button()
            if button:
                button.click()
        except Exception:
            logger.debug("cloudflare stage=click_verification_failed", exc_info=True)

    def is_bypassed(self) -> bool:
        try:
            title = (self.driver.title or "").lower()
            return not any(marker in title for marker in ("just a moment", "please wait", "请稍候", "請稍候"))
        except Exception:
            logger.debug("cloudflare stage=check_title_failed", exc_info=True)
            return False

    def bypass(self) -> None:
        attempts = 0
        while not self.is_bypassed():
            if 0 < self.max_retries <= attempts:
                break
            self.click_verification_button()
            attempts += 1
            time.sleep(2)
        if not self.is_bypassed():
            raise CloudflareBypassError(f"Cloudflare challenge still present after {attempts} attempts")


def env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, "").strip().lower()
    if not raw_value:
        return default
    return raw_value in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return max(minimum, value)


def cloudflare_bypass_enabled() -> bool:
    return env_bool("CF_BYPASS_ENABLED", True)


def cloudflare_semaphore() -> threading.BoundedSemaphore:
    global _cf_semaphore, _cf_semaphore_limit
    limit = env_int("CF_BYPASS_CONCURRENCY", 1)
    with _cf_semaphore_lock:
        if _cf_semaphore is None or _cf_semaphore_limit != limit:
            _cf_semaphore = threading.BoundedSemaphore(limit)
            _cf_semaphore_limit = limit
        return _cf_semaphore


def is_cloudflare_blocked(title: str, body_text: str, html: str = "") -> bool:
    haystack = f"{title}\n{body_text}\n{html[:30000]}".lower()
    return any(marker in haystack for marker in BLOCKED_MARKERS)


def playwright_chromium_candidates() -> list[str]:
    candidates: list[str] = []
    cache_roots = []
    if os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip():
        cache_roots.append(Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", "")).expanduser())
    cache_roots.append(Path.home() / ".cache" / "ms-playwright")
    for cache_root in cache_roots:
        if not str(cache_root) or not cache_root.exists():
            continue
        candidates.extend(str(path) for path in cache_root.glob("chromium-*/chrome-linux/chrome"))
    return candidates


def browser_candidates() -> list[str | None]:
    system = platform.system()
    if system == "Windows":
        paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        ]
    elif system == "Darwin":
        paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    return [os.getenv("CF_BROWSER_PATH"), os.getenv("CHROME_PATH"), *paths, *playwright_chromium_candidates()]


def resolve_browser_path() -> str:
    for candidate in browser_candidates():
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    raise CloudflareBypassError("No Chromium-based browser found. Set CF_BROWSER_PATH or CHROME_PATH.")


def extension_paths() -> tuple[str, str]:
    asset_dir = Path(__file__).resolve().parents[1] / "assets" / "cloudflare"
    turnstile = Path(os.getenv("CF_TURNSTILE_EXTENSION_PATH", asset_dir / "turnstilePatch"))
    ua_patch = Path(os.getenv("CF_UA_EXTENSION_PATH", asset_dir / "cloudflare_ua_patch"))
    missing = [str(path) for path in (turnstile, ua_patch) if not (path / "manifest.json").exists()]
    if missing:
        raise CloudflareBypassError(f"Missing Cloudflare browser extension(s): {', '.join(missing)}")
    return str(turnstile), str(ua_patch)


def chromium_runtime_args() -> list[str]:
    args: list[str] = []
    if platform.system() == "Linux":
        args.append("--disable-dev-shm-usage")
        if getattr(os, "geteuid", lambda: -1)() == 0:
            args.append("--no-sandbox")

    extra_args = os.getenv("CF_CHROMIUM_ARGS", "").strip()
    if extra_args:
        args.extend(shlex.split(extra_args))

    return list(dict.fromkeys(args))


def build_chromium_options(browser_path: str, headless: bool) -> Any:
    try:
        from DrissionPage import ChromiumOptions
    except ImportError as exc:
        raise CloudflareBypassError("DrissionPage is not installed. Run: pip install DrissionPage==4.1.0.17") from exc

    turnstile_patch, ua_patch = extension_paths()
    options = ChromiumOptions().auto_port()
    options.set_paths(browser_path=browser_path)
    options.headless(headless)
    options.set_argument("-accept-lang=zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7")
    options.set_argument("--lang=zh-CN")
    options.set_argument("--disable-blink-features=AutomationControlled")
    options.set_argument("--no-first-run")
    options.set_argument("--no-default-browser-check")
    options.set_argument("--disable-background-mode")
    options.set_argument("--deny-permission-prompts")
    options.set_argument("--window-size=1280,900")
    for arg in chromium_runtime_args():
        options.set_argument(arg)
    options.add_extension(turnstile_patch)
    options.add_extension(ua_patch)
    return options


def fetch_cloudflare_bypassed(url: str) -> tuple[str, str]:
    if not cloudflare_bypass_enabled():
        raise CloudflareBypassError("Cloudflare bypass is disabled by CF_BYPASS_ENABLED")

    try:
        from DrissionPage import ChromiumPage
    except ImportError as exc:
        raise CloudflareBypassError("DrissionPage is not installed. Run: pip install DrissionPage==4.1.0.17") from exc

    browser_path = resolve_browser_path()
    headless = env_bool("CF_BYPASS_HEADLESS", False)
    retries = env_int("CF_BYPASS_RETRIES", 8)
    wait_seconds = env_int("CF_BYPASS_WAIT", 8, minimum=0)
    page_timeout = env_int("CF_BYPASS_PAGE_TIMEOUT", 60)
    parsed_host = url.split("/", 3)[2] if "://" in url else url
    wait_started = time.monotonic()
    logger.warning("cloudflare stage=queued host=%s headless=%s", parsed_host, headless)

    with cloudflare_semaphore():
        started = time.monotonic()
        logger.warning(
            "cloudflare stage=start host=%s queue_wait=%.2fs browser=%s",
            parsed_host,
            started - wait_started,
            browser_path,
        )
        driver = ChromiumPage(addr_or_opts=build_chromium_options(browser_path, headless))
        try:
            ok = driver.get(url, timeout=page_timeout)
            if ok is False:
                raise CloudflareBypassError(f"Browser page load timed out after {page_timeout}s")
            CloudflareBypasser(driver, max_retries=retries).bypass()
            if wait_seconds:
                time.sleep(wait_seconds)
            html = driver.html or ""
            final_url = driver.url or url
            title = driver.title or ""
            if is_cloudflare_blocked(title, "", html):
                raise CloudflareBypassError(f"Cloudflare challenge page still detected: title={title!r}")
            logger.warning(
                "cloudflare stage=done host=%s final_url=%s bytes=%d elapsed=%.2fs",
                parsed_host,
                final_url,
                len(html),
                time.monotonic() - started,
            )
            return html, final_url
        finally:
            driver.quit()
