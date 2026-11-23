"""开发启动入口：先设置 Windows 事件循环策略，再启动 uvicorn。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.win_compat import setup_event_loop

setup_event_loop()

import uvicorn

from app.config import get_config
from app.main import app

if __name__ == "__main__":
    c = get_config()
    uvicorn.run(app, host=c.http_host, port=c.http_port, log_level="info", loop="none")
