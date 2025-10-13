# -*- coding: utf-8 -*-
"""
指令处理模块

职责：
- 将服务端通过 WebSocket 下发的 command 封包映射为对 WeChat 实例的调用
- 按协议发送 command_result（success/failed/timeout/rejected）
- 维护最近消息缓存，支持按消息 id/hash 进行 quote/forward 等操作
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple
import threading
import queue

from wxauto4.wx import WeChat, Chat  # 导入高层操作接口
from wxauto4.param import WxResponse


class CommandHandler:
    """指令处理器

    - 绑定一个 `WeChat` 实例与 `WsClient`（通过回调接口发送响应）
    - 暴露 `handle(payload)` 处理下行 JSON 指令
    - 通过 `register_message()` 存储最近消息，便于 quote/forward
    - 通过 `set_listen_callback()` 统一监听回调，供 add_listener 复用
    """

    def __init__(self, wx: WeChat, send_command_result: Callable[[str, str, Optional[Any], Optional[dict], Optional[str]], None]) -> None:
        """初始化

        参数：
        - wx: WeChat 实例
        - send_command_result: 发送 command_result 的函数，签名为 (command_id, status, result, error, trace_id)
        """
        self.wx = wx
        self._send_command_result = send_command_result
        self._listen_callback: Optional[Callable[[Any, Chat], None]] = None
        # 消息缓存：id/hash -> (msg, chat)
        self._msg_cache: Dict[str, Tuple[Any, Chat]] = {}

    # -------------------------- 基础能力 --------------------------
    def set_listen_callback(self, cb: Callable[[Any, Chat], None]) -> None:
        """设置消息监听回调（AddListenChat 时使用同一回调）"""
        self._listen_callback = cb

    def register_message(self, msg: Any, chat: Chat) -> None:
        """注册/更新收到的消息缓存，供后续 quote/forward 查询"""
        # 以 runtimeid 和 hash 双键冗余缓存，便于多种定位
        mid = getattr(msg, "id", None)
        if mid:
            self._msg_cache[str(mid)] = (msg, chat)
        mh = getattr(msg, "hash", None)
        if mh:
            self._msg_cache[str(mh)] = (msg, chat)

    def _send_ok(self, trace_id: str, command_id: str, result: Any = None) -> None:
        self._send_command_result(command_id, status="success", result=result or {}, error=None, trace_id=trace_id)

    def _send_failed(self, trace_id: str, command_id: str, code: str, message: str) -> None:
        self._send_command_result(command_id, status="failed", result={}, error={"code": code, "message": message}, trace_id=trace_id)

    def _send_timeout(self, trace_id: str, command_id: str) -> None:
        self._send_command_result(command_id, status="timeout", result={}, error={"code": "RPA_TIMEOUT", "message": "timeout"}, trace_id=trace_id)

    def _send_rejected(self, trace_id: str, command_id: str, message: str) -> None:
        self._send_command_result(command_id, status="rejected", result={}, error={"code": "INVALID_PARAMS", "message": message}, trace_id=trace_id)

    # -------------------------- 入口 --------------------------
    def handle(self, envelope: Dict[str, Any]) -> None:
        """处理下行 command 封包

        结构示例：
        {
          "type": "command",
          "traceId": "uuid",
          "deviceId": "wxrpa-001",
          "timestamp": 1739420800123,
          "payload": {
            "commandId": "uuid",
            "action": "send_text",
            "params": { ... },
            "timeoutMs": 8000
          }
        }
        """
        if envelope.get("type") != "command":
            return
        trace_id = envelope.get("traceId") or ""
        payload = envelope.get("payload") or {}
        command_id = payload.get("commandId") or ""
        action = payload.get("action") or ""
        params = payload.get("params") or {}
        timeout_ms = payload.get("timeoutMs")
        if not command_id or not action:
            self._send_rejected(trace_id, command_id or "", "缺少 commandId 或 action")
            return
        try:
            # 执行并处理超时
            def _exec():
                if action == "send_text":
                    return self._handle_send_text(params)
                elif action == "send_files":
                    return self._handle_send_files(params)
                elif action == "chat_with":
                    return self._handle_chat_with(params)
                elif action == "add_listener":
                    return self._handle_add_listener(params)
                elif action == "remove_listener":
                    return self._handle_remove_listener(params)
                elif action == "start_listening":
                    return self._handle_start_listening()
                elif action == "stop_listening":
                    return self._handle_stop_listening()
                elif action == "quote":
                    return self._handle_quote(params)
                elif action == "forward":
                    return self._handle_forward(params)
                else:
                    raise ValueError(f"未知指令: {action}")

            result = self._run_with_timeout(_exec, timeout_ms)
            self._send_ok(trace_id, command_id, result=result)
        except TimeoutError:
            self._send_timeout(trace_id, command_id)
        except ValueError as ve:
            self._send_rejected(trace_id, command_id, str(ve))
        except Exception as e:
            self._send_failed(trace_id, command_id, code="RPA_EXEC_ERROR", message=str(e))

    # -------------------------- 具体指令 --------------------------
    def _handle_send_text(self, params: Dict[str, Any]) -> Dict[str, Any]:
        to = params.get("to")
        text = params.get("text")
        if not text:
            raise ValueError("缺少 text")
        clear = params.get("clear", True)
        at = params.get("at")
        exact = params.get("exact", False)
        resp = self.wx.SendMsg(text, to, clear, at, exact)
        if not resp:
            raise RuntimeError(resp.get("message"))
        return {"message": resp.get("message")}

    def _handle_send_files(self, params: Dict[str, Any]) -> Dict[str, Any]:
        to = params.get("to")
        files = params.get("files") or params.get("file")
        exact = params.get("exact", False)
        if not files:
            raise ValueError("缺少 files")
        resp = self.wx.SendFiles(files, to, exact)
        if not resp:
            raise RuntimeError(resp.get("message"))
        return {"message": resp.get("message")}

    def _handle_chat_with(self, params: Dict[str, Any]) -> Dict[str, Any]:
        who = params.get("to") or params.get("who")
        exact = params.get("exact", True)
        force = params.get("force", False)
        force_wait = params.get("force_wait", 0.5)
        if not who:
            raise ValueError("缺少 to/who")
        result = self.wx.ChatWith(who, exact, force, force_wait)
        ok = bool(result) if result is not None else True
        if not ok:
            raise RuntimeError("切换会话失败")
        return {"result": True}

    def _handle_add_listener(self, params: Dict[str, Any]) -> Dict[str, Any]:
        who = params.get("who") or params.get("to")
        if not who:
            raise ValueError("缺少 who/to")
        if isinstance(who, list):
            errors = []
            for w in who:
                r = self.wx.AddListenChat(w, self._listen_callback) if self._listen_callback else self.wx.AddListenChat(w, lambda *_: None)
                if not r:
                    errors.append(w)
            if errors:
                raise RuntimeError(f"以下对象添加监听失败: {errors}")
            return {"result": True}
        else:
            r = self.wx.AddListenChat(who, self._listen_callback) if self._listen_callback else self.wx.AddListenChat(who, lambda *_: None)
            if not r:
                raise RuntimeError("添加监听失败")
            return {"result": True}

    def _handle_remove_listener(self, params: Dict[str, Any]) -> Dict[str, Any]:
        who = params.get("who") or params.get("to")
        if not who:
            raise ValueError("缺少 who/to")
        resp = self.wx.RemoveListenChat(who)
        if not resp:
            raise RuntimeError(resp.get("message"))
        return {"result": True}

    def _handle_start_listening(self) -> Dict[str, Any]:
        # 兼容未初始化监听线程的情况
        try:
            if hasattr(self.wx, "_listener_thread") and getattr(self.wx, "_listener_thread").is_alive():
                pass
            else:
                try:
                    self.wx.StartListening()
                except Exception:
                    self.wx._listener_start()
        except Exception:
            raise RuntimeError("启动监听失败")
        return {"result": True}

    def _handle_stop_listening(self) -> Dict[str, Any]:
        try:
            if hasattr(self.wx, "_listener_thread"):
                self.wx.StopListening()
        except Exception:
            raise RuntimeError("停止监听失败")
        return {"result": True}

    def _find_msg(self, ident: str) -> Optional[Tuple[Any, Chat]]:
        return self._msg_cache.get(str(ident))

    def _handle_quote(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ident = params.get("id") or params.get("hash")
        text = params.get("text")
        at = params.get("at")
        if not ident or not text:
            raise ValueError("缺少 id/hash 或 text")
        found = self._find_msg(ident)
        if not found:
            raise RuntimeError("未找到目标消息")
        msg, _ = found
        resp: WxResponse = msg.quote(text, at)
        if not resp:
            raise RuntimeError(resp.get("message"))
        return {"result": True}

    def _handle_forward(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ident = params.get("id") or params.get("hash")
        targets = params.get("targets") or params.get("who")
        if not ident or not targets:
            raise ValueError("缺少 id/hash 或 targets")
        found = self._find_msg(ident)
        if not found:
            raise RuntimeError("未找到目标消息")
        msg, _ = found
        resp: WxResponse = msg.forward(targets)
        if not resp:
            raise RuntimeError(resp.get("message"))
        return {"result": True}

    # -------------------------- 工具：带超时执行 --------------------------
    def _run_with_timeout(self, func: Callable[[], Any], timeout_ms: Optional[int]) -> Any:
        if not timeout_ms or timeout_ms <= 0:
            return func()
        q: "queue.Queue" = queue.Queue(maxsize=1)
        exc_holder: Dict[str, BaseException] = {}

        def _target():
            try:
                q.put(func())
            except BaseException as e:  # 传递异常
                exc_holder["e"] = e
                q.put(None)

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        try:
            result = q.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            raise TimeoutError()
        if "e" in exc_holder:
            raise exc_holder["e"]
        return result
