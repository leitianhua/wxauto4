# -*- coding: utf-8 -*-
"""
桥接运行入口

职责：
- 读取参数/环境变量，初始化 WeChat 实例
- 启动 WebSocket 桥接，与后端服务通讯
- 可选：预置监听对象列表

环境变量：
- WS_URL: WebSocket 服务端地址（必填或通过 --ws-url 传入）
- WS_LISTEN: 监听对象，逗号分隔，如：张三,项目群
"""
from __future__ import annotations

import argparse
import os
import time
from typing import List

from wxauto4.wx import WeChat
from . import start_ws_bridge


def _parse_listens(s: str | None) -> List[str]:
    if not s:
        return []
    return [i.strip() for i in s.split(",") if i.strip()]


def main() -> None:
    """命令行主入口"""
    parser = argparse.ArgumentParser(description="wxauto4 WebSocket 桥接入口")
    parser.add_argument("--ws-url", type=str, help="WebSocket 服务端地址，如 ws://127.0.0.1:8080/ws")
    parser.add_argument("--listen", type=str, help="预置监听对象，逗号分隔")
    parser.add_argument("--device-id", type=str, help="设备ID，用于协议 deviceId 标识")
    parser.add_argument("--debug", action="store_true", help="启用调试日志")
    args = parser.parse_args()

    ws_url = args.ws_url or os.getenv("WS_URL")
    if not ws_url:
        raise SystemExit("缺少 WS_URL，可通过 --ws-url 或环境变量 WS_URL 提供")

    default_listens = _parse_listens(args.listen or os.getenv("WS_LISTEN"))
    device_id = args.device_id or os.getenv("DEVICE_ID")

    # 初始化 WeChat，不主动启动监听器，避免 StartListening 属性缺失问题
    wx = WeChat(start_listener=False, debug=args.debug)

    # 启动桥接
    ws = start_ws_bridge(wx, ws_url=ws_url, default_listens=default_listens, device_id=device_id)

    try:
        # 保持进程常驻
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ws.stop()
        except Exception:
            pass
        try:
            wx.StopListening(True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
