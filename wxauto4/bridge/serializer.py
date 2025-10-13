# -*- coding: utf-8 -*-
"""
序列化模块

职责：
- 将消息对象与聊天上下文转换为可通过 WebSocket 传输的 dict
- 对不同消息类型的附加字段做兼容性处理
"""
from __future__ import annotations

import time
from typing import Any, Dict


def _now_ms() -> int:
    """当前时间戳（毫秒）"""
    return int(time.time() * 1000)


def serialize_message(msg: Any) -> Dict[str, Any]:
    """序列化消息对象

    返回字段：
    - id, hash, type, attr, content, direction, distince
    - 可选：quote_nickname, quote_content（当为引用消息）
    """
    data: Dict[str, Any] = {
        "id": getattr(msg, "id", None),
        "hash": getattr(msg, "hash", None),
        "type": getattr(msg, "type", None),
        "attr": getattr(msg, "attr", None),
        "content": getattr(msg, "content", None),
        "direction": getattr(msg, "direction", None),
        "distince": getattr(msg, "distince", None),
    }
    # 引用消息的扩展字段
    for k in ("quote_nickname", "quote_content"):
        if hasattr(msg, k):
            data[k] = getattr(msg, k)
    return data


def serialize_chat_info(chat: Any) -> Dict[str, Any]:
    """序列化聊天上下文信息（来源于 Chat.ChatInfo()）"""
    try:
        info = chat.ChatInfo()
    except Exception:
        info = {}
    # who 为窗口昵称（便于服务端匹配）
    info["who"] = getattr(chat, "who", None)
    return info


def pack_message_event(msg: Any, chat: Any) -> Dict[str, Any]:
    """打包上行消息事件数据（对齐后端协议 data 格式）

    返回 data：
    {
      "messageId": "...",
      "from": "...",
      "chatId": "...",
      "msgType": "text|image|file|system|video|voice|quote|other",
      "content": "...",
      "raw": { 原始可调试数据 }
    }
    """
    raw_msg = serialize_message(msg)
    raw_chat = serialize_chat_info(chat)
    message_id = raw_msg.get("hash") or raw_msg.get("id") or f"msg-{_now_ms()}"
    # 由于当前无法获取微信真实 ID，这里使用聊天名作为 from/chatId 的占位
    chat_name = raw_chat.get("chat_name") or raw_chat.get("who")
    sender = raw_msg.get("attr")  # friend/self/system
    msg_type = raw_msg.get("type") or "other"
    content = raw_msg.get("content")

    return {
        "messageId": str(message_id),
        "from": str(chat_name) if chat_name is not None else None,
        "chatId": str(chat_name) if chat_name is not None else None,
        "msgType": str(msg_type),
        "content": content,
        "raw": {
            "message": raw_msg,
            "chat": raw_chat,
            "ts": _now_ms(),
            "sender": sender,
        },
    }
