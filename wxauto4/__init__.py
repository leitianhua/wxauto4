import sys

# 仅在 Windows 平台导出 WeChat，便于在 macOS 仅使用网络组件（如 WsClient）
if sys.platform == 'win32':
    from .wx import WeChat  # noqa: F401