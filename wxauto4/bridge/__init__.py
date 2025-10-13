# -*- coding: utf-8 -*-
"""
桥接包入口

职责：
- 启动 WebSocket 客户端，与后端保持连接
- 将微信消息监听回调与 WS 事件上报对接
- 将服务端指令转为对 WeChat 的操作
"""
from __future__ import annotations

from typing import Iterable, Optional

from wxauto4.wx import WeChat, Chat

from .ws_client import WsClient
from .serializer import pack_message_event
from .commands import CommandHandler


def start_ws_bridge(wx: WeChat, ws_url: str, default_listens: Optional[Iterable[str]] = None, device_id: Optional[str] = None) -> WsClient:
    """启动 WebSocket 桥接

    参数：
    - wx: 已登录/就绪的 WeChat 实例
    - ws_url: WebSocket 服务端地址（ws:// 或 wss://）
    - default_listens: 启动时预置监听的会话列表（好友/群昵称）

    返回：
    - WsClient 实例，可用于停止连接等
    """

    # 1) 先占位创建 ws，便于在 handler 中回调 send_reply
    ws_client = WsClient(url=ws_url, device_id=device_id)

    # 2) 指令处理器，负责把下行 action -> WeChat 操作
    handler = CommandHandler(wx, send_command_result=ws_client.send_command_result)

    # 3) 监听回调：
    def _on_message(msg, chat: Chat):
        # 注册消息到缓存，便于后续 quote/forward
        handler.register_message(msg, chat)
        # 打包并上报事件
        payload = pack_message_event(msg, chat)
        ws_client.send_event("wechat_message", payload)

    handler.set_listen_callback(_on_message)

    # 4) 将 WS 下行与 handler 对接
    ws_client.on_command = handler.handle

    # 5) 启动 WS 连接
    ws_client.start()

    # 6) 可选：预置监听对象
    if default_listens:
        for who in default_listens:
            wx.AddListenChat(who, _on_message)

    return ws_client
