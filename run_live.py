"""多账号启动器：加载账号仓库，常驻 WebSocket、订单轮询与内部 API。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from loguru import logger

from xianyu_bridge.env import load_env


ROOT = Path(__file__).resolve().parent
load_env(ROOT / ".env")

from xianyu_bridge.account_login import qr_login_and_save
from xianyu_bridge.api import app
from xianyu_bridge.runtime import runtime


LEGACY_COOKIE_FILE = ROOT / ".cookies_str"


async def main() -> None:
    legacy_id = await asyncio.to_thread(runtime.account_store.import_legacy, LEGACY_COOKIE_FILE)
    if legacy_id:
        logger.info("[旧账号已兼容导入] 账号：{}", legacy_id)

    if not runtime.account_store.list_ids():
        logger.info("尚无闲鱼账号，进入首次扫码登录")
        await asyncio.to_thread(qr_login_and_save, runtime.account_store)

    await runtime.start()
    logger.info(
        "[闲鱼桥接启动] 账号数：{}，监听：{}:{}",
        len(runtime.account_store.list_ids()),
        os.getenv("XIANYU_HOST", "127.0.0.1"),
        os.getenv("XIANYU_PORT", "8090"),
    )

    try:
        import uvicorn

        config = uvicorn.Config(
            app,
            host=os.getenv("XIANYU_HOST", "127.0.0.1"),
            port=int(os.getenv("XIANYU_PORT", "8090")),
            log_level=os.getenv("XIANYU_LOG_LEVEL", "info").lower(),
        )
        await uvicorn.Server(config).serve()
    finally:
        await runtime.stop()


if __name__ == "__main__":
    asyncio.run(main())
