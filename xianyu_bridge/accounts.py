from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from utils.goofish_utils import trans_cookies


_SAFE_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class AccountStore:
    """按闲鱼账号隔离保存 Cookie，不在日志或接口中暴露内容。"""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def save(self, cookies: str) -> str:
        cookies = cookies.strip()
        account_id = str(trans_cookies(cookies).get("unb") or "").strip()
        self._validate_id(account_id)
        if not cookies:
            raise ValueError("Cookie 为空")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        target = self.path(account_id)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{account_id}.", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(cookies)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return account_id

    def load_all(self) -> dict[str, str]:
        if not self.root.exists():
            return {}
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            return {}
        result: dict[str, str] = {}
        for path in sorted(self.root.glob("*.cookies")):
            if path.is_symlink() or not path.is_file():
                continue
            account_id = path.stem
            try:
                self._validate_id(account_id)
                os.chmod(path, 0o600)
                cookies = path.read_text(encoding="utf-8").strip()
                cookie_account = str(trans_cookies(cookies).get("unb") or "")
                if cookie_account != account_id:
                    continue
                result[account_id] = cookies
            except (OSError, ValueError):
                continue
        return result

    def list_ids(self) -> list[str]:
        return list(self.load_all())

    def remove(self, account_id: str) -> bool:
        path = self.path(account_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def import_legacy(self, path: str | Path) -> str | None:
        legacy = Path(path)
        if not legacy.is_file() or legacy.is_symlink():
            return None
        try:
            os.chmod(legacy, 0o600)
            cookies = legacy.read_text(encoding="utf-8").strip()
            account_id = str(trans_cookies(cookies).get("unb") or "").strip()
            self._validate_id(account_id)
            if self.path(account_id).exists():
                return None
            return self.save(cookies) if cookies else None
        except (OSError, ValueError):
            return None

    def path(self, account_id: str) -> Path:
        self._validate_id(account_id)
        return self.root / f"{account_id}.cookies"

    @staticmethod
    def fingerprint(cookies: str) -> str:
        return hashlib.sha256(cookies.encode()).hexdigest()

    @staticmethod
    def _validate_id(account_id: str) -> None:
        if not account_id or not _SAFE_ACCOUNT_ID.fullmatch(account_id):
            raise ValueError("Cookie 中缺少合法的 unb 账号标识")
