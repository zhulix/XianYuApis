from pathlib import Path

from xianyu_bridge.env import load_env


load_env(Path(__file__).resolve().parent / ".env")

from xianyu_bridge.api import app

__all__ = ["app"]


if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run(app, host=os.getenv("XIANYU_HOST", "127.0.0.1"), port=int(os.getenv("XIANYU_PORT", "8090")))
