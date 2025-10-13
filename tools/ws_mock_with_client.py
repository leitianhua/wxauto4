# -*- coding: utf-8 -*-
"""
交互式协议模拟器（macOS 可运行，无需微信）

功能：
- 使用仓内的 wxauto4.bridge.ws_client.WsClient 连接后端 WebSocket
- 控制台交互：输入文本即可向服务端发送上行事件（wechat_message）
- 自动处理下行 command：自动发送 ack，并根据 action 返回 command_result
- 可选周期性发送事件（--tick 秒），方便联调

用法示例：
    export WS_URL=ws://127.0.0.1:8080/ws
    export DEVICE_ID=wxrpa-001
    PYTHONPATH=$(pwd) python tools/ws_mock_with_client.py --tick 0

控制台命令：
- 直接输入文本：发送 wechat_message 事件，content=该文本
- /event <msgType> <text...>  显式发送事件，指定 msgType（text|image|file|system），msgType 可省略
- /set msgType <type>        设置默认消息类型
- /json <envelope_json>      发送自定义原始 JSON 报文（高级场景）
- /help                      显示帮助
- /quit                      退出
"""
from __future__ import annotations

import os
import sys
import json
import time
import uuid
import threading
import argparse
from typing import Dict, Any, Optional
import logging

# 仅使用网络层，不导入任何 Win32/WeChat 相关模块
from wxauto4.bridge.ws_client import WsClient


def now_ms() -> int:
    """当前毫秒时间戳"""
    return int(time.time() * 1000)


class InteractiveMock:
    """交互式协议模拟器核心类

    职责：
    - 维护 WsClient 连接
    - 控制台输入解析与事件发送
    - 下行 command 自动应答（ack + command_result）
    """

    def __init__(self, url: str, device_id: str, tick: int = 0, default_msg_type: str = "text", device_in: str = "query", header_name: str = "X-Device-Id") -> None:
        """初始化

        参数：
        - url: WebSocket 服务端 URL
        - device_id: 设备 ID（出现在协议 envelop 中）
        - tick: 周期性事件发送间隔（秒），0 表示不自动发送
        - default_msg_type: 默认事件消息类型
        """
        self.url = url
        self.device_id = device_id
        self.tick = max(0, int(tick))
        self.default_msg_type = default_msg_type
        self.device_in = device_in
        self.header_name = header_name

        # 统一 WsClient 日志到 STDOUT
        logger = logging.getLogger("wxauto4.ws")
        logger.handlers.clear()
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)

        self.ws = WsClient(
            url=self.url,
            device_id=self.device_id,
            on_command=self._on_command,
            include_device_in=self.device_in,
            header_name=self.header_name,
            enable_log=True,
        )
        self._running = threading.Event()

    # -------------------- 生命周期 --------------------
    def start(self) -> None:
        """启动连接与交互线程"""
        self._running.set()
        self.ws.start()
        if self.tick > 0:
            threading.Thread(target=self._auto_event_loop, daemon=True).start()
        threading.Thread(target=self._stdin_loop, daemon=True).start()
        self._print_banner()
        print(f"[i] deviceId={self.device_id} deviceIn={self.device_in} url={self.url}")

        try:
            while self._running.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        """停止运行"""
        self._running.clear()
        try:
            self.ws.stop()
        except Exception:
            pass

    # -------------------- 上行：事件发送 --------------------
    def _send_wechat_message(self, content: str, msg_type: Optional[str] = None) -> None:
        """发送一条 wechat_message 上行事件"""
        mtype = (msg_type or self.default_msg_type).strip() or "text"
        data = self._build_wechat_message(content=content, msg_type=mtype)
        trace_id = str(uuid.uuid4())
        self.ws.send_event("wechat_message", data, trace_id=trace_id)
        print(f"[↑ event] traceId={trace_id} wechat_message: type={mtype}, content={content}")

    def _build_wechat_message(self, content: str = "对方发来文本", msg_type: str = "text") -> Dict[str, Any]:
        """构造后端协议要求的 data 部分"""
        return {
            "messageId": f"wxmsg-{uuid.uuid4()}",
            "from": "interactive-mock-user",
            "chatId": "interactive-mock-chat",
            "msgType": msg_type,  # text|image|file|system
            "content": content,
            "raw": {}
        }

    # -------------------- 下行：处理 command --------------------
    def _on_command(self, envelope: Dict[str, Any]) -> None:
        """WsClient 下行回调：处理 type=command（WsClient 已自动 ack）"""
        if envelope.get("type") != "command":
            print(f"[↓ other ] {json.dumps(envelope, ensure_ascii=False)}")
            return
        trace_id = envelope.get("traceId")
        payload = envelope.get("payload") or {}
        command_id = payload.get("commandId")
        action = payload.get("action")
        params = payload.get("params") or {}
        timeout_ms = payload.get("timeoutMs")

        print(f"[↓ command] traceId={trace_id} commandId={command_id} action={action} params={json.dumps(params, ensure_ascii=False)} timeoutMs={timeout_ms}")

        # 模拟超时
        if timeout_ms and timeout_ms < 100:
            time.sleep(timeout_ms / 1000.0 + 0.2)
            self.ws.send_command_result(command_id, status="timeout", trace_id=trace_id)
            print(f"[↑ result] traceId={trace_id} commandId={command_id} status=timeout")
            return

        # 简单映射：统一 success
        if action in ("send_text", "send_files", "chat_with", "add_listener",
                      "remove_listener", "start_listening", "stop_listening",
                      "quote", "forward"):
            result = {"echoParams": params, "messageId": f"mock-{uuid.uuid4()}"}
            self.ws.send_command_result(command_id, status="success", result=result, trace_id=trace_id)
            print(f"[↑ result] traceId={trace_id} commandId={command_id} status=success")
        else:
            self.ws.send_command_result(
                command_id,
                status="rejected",
                error={"code": "INVALID_ACTION", "message": f"未知指令: {action}"},
                trace_id=trace_id,
            )
            print(f"[↑ result] traceId={trace_id} commandId={command_id} status=rejected")

    # -------------------- 辅助：控制台交互 --------------------
    def _print_banner(self) -> None:
        print("""
================ 交互式协议模拟器 =================
- 直接输入文本：发送 wechat_message 事件
- /event <msgType> <text...>   显式发送事件（msgType 可省略）
- /set msgType <type>          设置默认消息类型（text|image|file|system）
- /json <envelope_json>        直接发送自定义 JSON 报文
- /help                        显示帮助
- /quit                        退出
==================================================
""".strip())

    def _stdin_loop(self) -> None:
        """控制台输入循环：解析命令并执行"""
        while self._running.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                break
            if not line:
                time.sleep(0.1)
                continue
            line = line.strip()
            if not line:
                continue
            if line in ("/quit", ":q", "exit"):
                self.stop()
                break
            if line in ("/help", "-h", "--help"):
                self._print_banner()
                continue

            if line.startswith("/set "):
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "msgType":
                    self.default_msg_type = parts[2]
                    print(f"[i] default msgType = {self.default_msg_type}")
                else:
                    print("[!] 用法：/set msgType <type>")
                continue

            if line.startswith("/event"):
                parts = line.split(maxsplit=2)
                if len(parts) == 1:
                    print("[!] 用法：/event <msgType> <text...> 或 /event <text...>")
                    continue
                if len(parts) == 2:
                    # 仅有文本，使用默认类型
                    self._send_wechat_message(parts[1], None)
                else:
                    # 明确指定 msgType 与文本
                    msg_type = parts[1]
                    text = parts[2]
                    # 若 msgType 看起来不像类型（太长），当作文本
                    if len(msg_type) > 12:
                        self._send_wechat_message(parts[1] + " " + parts[2], None)
                    else:
                        self._send_wechat_message(text, msg_type)
                continue

            if line.startswith("/json "):
                payload = line[len("/json "):].strip()
                try:
                    obj = json.loads(payload)
                except Exception as e:
                    print(f"[!] JSON 解析失败: {e}")
                    continue
                # 直接发送原文
                try:
                    self.ws._queue_send(obj)  # 复用底层发送
                    print("[↑ raw  ] 自定义 JSON 已发送")
                except Exception as e:
                    print(f"[!] 发送失败: {e}")
                continue

            # 默认：把整行当作文本事件
            self._send_wechat_message(line, None)

    # -------------------- 自动事件循环 --------------------
    def _auto_event_loop(self) -> None:
        while self._running.is_set():
            try:
                self._send_wechat_message(content="自动心跳事件", msg_type=self.default_msg_type)
            except Exception:
                pass
            t0 = time.time()
            while self._running.is_set() and time.time() - t0 < self.tick:
                time.sleep(0.2)


def main() -> None:
    parser = argparse.ArgumentParser(description="交互式协议模拟器（使用 WsClient）")
    parser.add_argument("--ws-url", type=str, help="WebSocket 服务端地址，如 ws://127.0.0.1:8080/ws")
    parser.add_argument("--device-id", type=str, default=os.getenv("DEVICE_ID", "wxrpa-macos"))
    parser.add_argument("--tick", type=int, default=0, help="周期性事件发送间隔（秒），0 表示关闭")
    parser.add_argument("--type", dest="msg_type", type=str, default="text", help="默认消息类型")
    args = parser.parse_args()

    ws_url = args.ws_url or os.getenv("WS_URL")
    if not ws_url:
        raise SystemExit("缺少 WS_URL，可通过 --ws-url 或环境变量 WS_URL 提供")

    mock = InteractiveMock(url=ws_url, device_id=args.device_id, tick=args.tick, default_msg_type=args.msg_type)
    mock.start()


if __name__ == "__main__":
    main()
