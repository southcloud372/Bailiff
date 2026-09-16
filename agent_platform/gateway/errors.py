"""网关领域的异常。"""


class GatewayError(Exception):
    """网关层错误的基类。"""


class PermissionDenied(GatewayError):
    """调用方无权访问该工具。"""


class UnknownTool(GatewayError):
    """请求的工具未注册。"""
