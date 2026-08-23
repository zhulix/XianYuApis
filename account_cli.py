"""闲鱼多账号管理：登录、查看和移除账号。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from xianyu_bridge.account_login import qr_login_and_save
from xianyu_bridge.accounts import AccountStore


ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="闲鱼桥接账号管理")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("login", help="扫码登录或刷新一个账号")
    subcommands.add_parser("list", help="列出已保存账号")
    remove = subcommands.add_parser("remove", help="移除一个账号")
    remove.add_argument("account_id")
    args = parser.parse_args()

    store = AccountStore(os.getenv("XIANYU_ACCOUNTS_DIR", str(ROOT / "data" / "accounts")))
    if args.command == "login":
        account_id = qr_login_and_save(store)
        print(json.dumps({"success": True, "accountId": account_id}, ensure_ascii=False))
    elif args.command == "list":
        print(json.dumps({"success": True, "accounts": store.list_ids()}, ensure_ascii=False))
    elif args.command == "remove":
        removed = store.remove(args.account_id)
        print(json.dumps({"success": removed, "accountId": args.account_id}, ensure_ascii=False))


if __name__ == "__main__":
    main()
