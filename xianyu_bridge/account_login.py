from __future__ import annotations

from loguru import logger

from goofish_apis import qrcode_login

from .accounts import AccountStore


def session_cookies_str(api) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for cookie in api.session.cookies:
        if cookie.name in seen or not cookie.value:
            continue
        if cookie.domain and ("goofish.com" in cookie.domain or "mmstat.com" in cookie.domain):
            seen.add(cookie.name)
            parts.append(f"{cookie.name}={cookie.value}")
    return "; ".join(parts)


def qr_login_and_save(store: AccountStore, attempts: int = 10) -> str:
    api = None
    for attempt in range(1, attempts + 1):
        try:
            api = qrcode_login(show_qrcode=True)
            break
        except TimeoutError as exc:
            logger.warning("第 {} 次二维码未扫码或已过期：{}", attempt, exc)
    if api is None:
        raise RuntimeError("多次扫码超时，请重新执行登录命令")
    account_id = store.save(session_cookies_str(api))
    logger.info("[闲鱼账号保存] 账号：{}，目录：{}", account_id, store.root)
    return account_id
