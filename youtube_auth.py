from email.utils import parsedate_to_datetime
from http.cookiejar import Cookie, MozillaCookieJar
from http.cookies import Morsel
import os
import tempfile
import threading
from urllib.parse import urlparse


YOUTUBE_LOGIN_COOKIES = {
    "SAPISID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "SID",
    "LOGIN_INFO",
}


def _make_cookie(name: str, morsel: Morsel, default_domain: str) -> Cookie:
    domain = str(morsel["domain"] or default_domain).lower()
    path = str(morsel["path"] or "/")
    expires_value = str(morsel["expires"] or "")
    expires = None
    if expires_value:
        expires_timestamp = int(parsedate_to_datetime(expires_value).timestamp())
        if expires_timestamp > 0:
            expires = expires_timestamp

    rest: dict[str, str | None] = {}
    if morsel["httponly"]:
        rest["HttpOnly"] = None
    if morsel["samesite"]:
        rest["SameSite"] = str(morsel["samesite"])

    return Cookie(
        version=0,
        name=name,
        value=str(morsel.value),
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(morsel["domain"]),
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=bool(morsel["path"]),
        secure=bool(morsel["secure"]),
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest=rest,
        rfc2109=False,
    )


def save_youtube_cookies(window, cookie_path: str) -> bool:
    current_url = window.get_current_url()
    current_host = urlparse(current_url).hostname if current_url else None
    if current_host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return False

    youtube_cookies: list[Cookie] = []
    for cookie_container in window.get_cookies():
        for name, morsel in cookie_container.items():
            cookie_domain = str(morsel["domain"] or current_host).lstrip(".").lower()
            if (
                cookie_domain == "youtube.com"
                or cookie_domain.endswith(".youtube.com")
                or cookie_domain == "google.com"
                or cookie_domain.endswith(".google.com")
            ):
                youtube_cookies.append(_make_cookie(name, morsel, current_host))

    if not any(cookie.name in YOUTUBE_LOGIN_COOKIES for cookie in youtube_cookies):
        return False

    temp_fd, temp_path = tempfile.mkstemp(
        prefix=".youtube-cookies-",
        suffix=".tmp",
        dir=os.path.dirname(cookie_path)
    )
    os.close(temp_fd)
    try:
        cookie_jar = MozillaCookieJar(temp_path)
        for cookie in youtube_cookies:
            cookie_jar.set_cookie(cookie)
        cookie_jar.save(ignore_discard=True, ignore_expires=True)
        os.replace(temp_path, cookie_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return True


def run_youtube_login(cookie_path: str, storage_path: str) -> tuple[bool, str]:
    import webview
    from webview.errors import WebViewException

    os.makedirs(os.path.dirname(cookie_path), exist_ok=True)
    os.makedirs(storage_path, exist_ok=True)

    try:
        window = webview.create_window(
            "Вход в YouTube — YouTube Downloader Studio",
            "https://www.youtube.com/",
            width=1000,
            height=720,
            min_size=(720, 520),
        )
    except (WebViewException, OSError, RuntimeError, ValueError) as error:
        return False, str(error)
    saved = threading.Event()
    save_lock = threading.Lock()
    workers_lock = threading.Lock()
    save_workers: list[threading.Thread] = []
    save_errors: list[str] = []

    def save_after_navigation() -> None:
        if not save_lock.acquire(blocking=False):
            return
        try:
            if save_youtube_cookies(window, cookie_path):
                saved.set()
        except (OSError, ValueError, AttributeError, RuntimeError, WebViewException) as error:
            save_errors.append(str(error))
        finally:
            save_lock.release()

    def on_page_loaded() -> None:
        worker = threading.Thread(target=save_after_navigation, daemon=True)
        with workers_lock:
            save_workers.append(worker)
        worker.start()

    window.events.loaded += on_page_loaded
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=storage_path)
    except (WebViewException, OSError, RuntimeError) as error:
        return False, str(error)

    with workers_lock:
        workers = tuple(save_workers)
    for worker in workers:
        worker.join(timeout=10)

    if saved.is_set():
        return True, ""
    if save_errors:
        return False, save_errors[-1]
    return False, "Не найдена активная сессия YouTube. Войдите и дождитесь возврата на YouTube."
