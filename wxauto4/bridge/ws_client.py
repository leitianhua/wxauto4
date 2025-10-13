# -*- coding: utf-8 -*-
"""
WebSocket 客户端模块

职责：
- 维护与后端服务的 WebSocket 长连接（含自动重连、心跳）
- 向服务端发送事件与响应（线程安全、带队列）
- 接收服务端下行指令并回调分发给命令处理器

注意：
- 采用 websocket-client（同步）实现，避免与现有监听线程模型冲突
- 发送采用队列与单发送线程，保证线程安全与顺序性
"""
from __future__ import annotations

import json
import threading
import time
import queue
import uuid
from typing import Any, Callable, Optional
import logging
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

try:
    import websocket  # type: ignore
except Exception as e:  # pragma: no cover
    websocket = None


class WsClient:
    """WebSocket 客户端

    - 通过 `start()` 建立与后端的长连接
    - 通过 `send_event()`、`send_reply()` 发送上行消息
    - 通过设置 `on_command` 回调处理服务端指令
    """

    def __init__(
        self,
        url: str,
        on_command: Optional[Callable[[dict], None]] = None,
        ping_interval: int = 20,
        ping_timeout: int = 10,
        reconnect_interval: int = 5,
        device_id: Optional[str] = None,
        include_device_in: str = "none",  # none|query|header
        header_name: str = "X-Device-Id",
        enable_log: bool = True,
    ) -> None:
        """初始化客户端

        参数：
        - url: WebSocket 服务端地址（ws:// 或 wss://）
        - on_command: 下行指令回调，入参为已解析的 JSON dict
        - ping_interval: 心跳间隔秒
        - ping_timeout: 心跳超时秒
        - reconnect_interval: 重连间隔秒
        """
        self.url = url
        self.on_command = on_command
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.reconnect_interval = reconnect_interval
        self.device_id = device_id or "wxrpa-unknown"
        self._device_in = include_device_in
        self._header_name = header_name
        # 日志器
        self._logger = logging.getLogger("wxauto4.ws")
        if not self._logger.handlers:
            _h = logging.StreamHandler()
            _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            self._logger.addHandler(_h)
        self._logger.setLevel(logging.INFO)
        self._log_enabled = bool(enable_log)

        self._ws_app: Optional["websocket.WebSocketApp"] = None
        self._run_thread: Optional[threading.Thread] = None
        self._send_thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._connected = threading.Event()

        self._send_queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.RLock()

    # -------------------------- 对外生命周期 --------------------------
    def start(self) -> None:
        """启动连接与发送线程"""
        if websocket is None:
            raise RuntimeError("websocket-client 未安装，请先安装依赖 'websocket-client'")
        if self._running.is_set():
            return
        self._running.set()
        self._run_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._run_thread.start()
        self._send_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._send_thread.start()

    def stop(self) -> None:
        """停止连接与发送线程"""
        if not self._running.is_set():
            return
        self._running.clear()
        try:
            if self._ws_app:
                try:
                    self._ws_app.close()
                except Exception:
                    pass
        finally:
            if self._run_thread and self._run_thread.is_alive():
                self._run_thread.join(timeout=2)
            if self._send_thread and self._send_thread.is_alive():
                self._send_thread.join(timeout=2)
            self._connected.clear()

    # -------------------------- 发送 API --------------------------
    def send_event(self, event_type: str, data: Any, trace_id: Optional[str] = None) -> None:
        """发送事件（对齐协议）
        上行 envelop:
        {
          "type": "event",
          "traceId": "uuid",
          "deviceId": "...",
          "timestamp": 1739420801123,
          "payload": { "eventType": event_type, "data": data }
        }
        """
        _tid = trace_id or str(uuid.uuid4())
        envelope = {
            "type": "event",
            "traceId": _tid,
            "deviceId": self.device_id,
            "timestamp": int(time.time() * 1000),
            "payload": {
                "eventType": event_type,
                "data": data,
            },
        }
        self._queue_send(envelope)
        if self._log_enabled:
            try:
                self._logger.info(f"[send][event] traceId=%s eventType=%s", _tid, event_type)
            except Exception:
                pass

    def send_reply(self, reply_to: str, ok: bool, error: Optional[str], data: Any = None) -> None:
        """兼容旧接口：保留但不推荐"""
        payload = {
            "reply_to": reply_to,
            "ok": bool(ok),
            "error": error,
            "data": data,
        }
        self._queue_send(payload)

    def send_ack(self, for_type: str, for_id: str, trace_id: Optional[str] = None) -> None:
        """发送 ack 确认"""
        _tid = trace_id or str(uuid.uuid4())
        envelope = {
            "type": "ack",
            "traceId": _tid,
            "deviceId": self.device_id,
            "timestamp": int(time.time() * 1000),
            "payload": {"forType": for_type, "forId": for_id},
        }
        self._queue_send(envelope)
        if self._log_enabled:
            try:
                self._logger.info(f"[send][ack] traceId=%s forType=%s forId=%s", _tid, for_type, for_id)
            except Exception:
                pass

    def send_error(self, for_type: str, for_id: str, code: str, message: str, trace_id: Optional[str] = None) -> None:
        """发送错误通知"""
        _tid = trace_id or str(uuid.uuid4())
        envelope = {
            "type": "error",
            "traceId": _tid,
            "deviceId": self.device_id,
            "timestamp": int(time.time() * 1000),
            "payload": {"forType": for_type, "forId": for_id, "code": code, "message": message},
        }
        self._queue_send(envelope)
        if self._log_enabled:
            try:
                self._logger.info(f"[send][error] traceId=%s forType=%s forId=%s code=%s", _tid, for_type, for_id, code)
            except Exception:
                pass

    def send_command_result(
        self,
        command_id: str,
        status: str,
        result: Optional[Any] = None,
        error: Optional[dict] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        """发送 command_result"""
        _tid = trace_id or str(uuid.uuid4())
        envelope = {
            "type": "command_result",
            "traceId": _tid,
            "deviceId": self.device_id,
            "timestamp": int(time.time() * 1000),
            "payload": {
                "commandId": command_id,
                "status": status,
                "result": result or {},
                "error": error,
            },
        }
        self._queue_send(envelope)
        if self._log_enabled:
            try:
                self._logger.info(f"[send][command_result] traceId=%s commandId=%s status=%s", _tid, command_id, status)
            except Exception:
                pass

    # -------------------------- 内部实现 --------------------------
    def _queue_send(self, obj: Any) -> None:
        try:
            text = json.dumps(obj, ensure_ascii=False)
        except Exception:
            # 尝试降级：不可序列化则只发简要错误
            text = json.dumps({"event": "serialize_error"}, ensure_ascii=False)
        self._send_queue.put(text)

    def _sender_loop(self) -> None:
        """发送线程：从队列取消息并发送"""
        while self._running.is_set():
            try:
                text = self._send_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if self._connected.is_set() and self._ws_app and self._ws_app.sock and self._ws_app.sock.connected:
                    self._ws_app.send(text)
                else:
                    # 未连接时丢回队列前部的简单策略：延迟后再入队
                    time.sleep(0.5)
                    self._send_queue.put(text)
            except Exception:
                # 发送失败重入队列，等待重连
                time.sleep(0.5)
                self._send_queue.put(text)

    def _run_loop(self) -> None:
        """连接与重连主循环"""
        while self._running.is_set():
            try:
                self._connected.clear()
                # 计算本次连接 URL 与 Header
                connect_url = self.url
                headers = None
                if self._device_in == "query":
                    pu = urlparse(self.url)
                    q = dict(parse_qsl(pu.query, keep_blank_values=True))
                    q["deviceId"] = self.device_id
                    new_query = urlencode(q)
                    connect_url = urlunparse((pu.scheme, pu.netloc, pu.path, pu.params, new_query, pu.fragment))
                elif self._device_in == "header":
                    headers = [f"{self._header_name}: {self.device_id}"]
                if self._log_enabled:
                    try:
                        self._logger.info("[connect] url=%s deviceIn=%s deviceId=%s", connect_url, self._device_in, self.device_id)
                    except Exception:
                        pass
                self._ws_app = websocket.WebSocketApp(
                    connect_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                    header=headers,
                )
                # run_forever 支持心跳与自动重连（网络异常时退出本次，随后我们手动 sleep 再重试）
                self._ws_app.run_forever(
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                )
            except Exception:
                pass
            finally:
                self._connected.clear()
                if not self._running.is_set():
                    break
                time.sleep(self.reconnect_interval)

    # -------------------------- WS 回调 --------------------------
    def _on_open(self, ws):  # noqa: ANN001
        self._connected.set()

    def _on_close(self, ws, close_status_code, close_msg):  # noqa: ANN001
        self._connected.clear()

    def _on_error(self, ws, error):  # noqa: ANN001
        # 忽略，重连逻辑在 _run_loop 中统一处理
        pass

    def _on_message(self, ws, message: str):  # noqa: ANN001
        try:
            data = json.loads(message)
        except Exception:
            return
        # 记录收到的报文类型与 traceId
        if self._log_enabled and isinstance(data, dict):
            try:
                _t = data.get("type")
                _tid = data.get("traceId")
                if _t == "command":
                    _cid = (data.get("payload") or {}).get("commandId")
                    self._logger.info(f"[recv][command] traceId=%s commandId=%s", _tid, _cid)
                else:
                    self._logger.info(f"[recv][%s] traceId=%s", str(_t), _tid)
            except Exception:
                pass
        # 自动处理下行 command：发送 ack 后分发
        if isinstance(data, dict) and data.get("type") == "command":
            try:
                payload = data.get("payload") or {}
                command_id = payload.get("commandId")
                if command_id:
                    self.send_ack(for_type="command", for_id=command_id, trace_id=data.get("traceId"))
            except Exception:
                pass
            if callable(self.on_command):
                try:
                    self.on_command(data)
                except Exception:
                    pass
            return
        # 其余类型，透传给 on_command 以便上层扩展
        if callable(self.on_command):
            try:
                self.on_command(data)
            except Exception:
                pass
