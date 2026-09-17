"""LoggerService and Logger (port of ``packages/core/src/logger.ts``).

``LoggerService`` is a callable service: calling ``ctx.logger('name')`` returns a
:class:`Logger`.  The call route goes through ``symbols.invoke`` and takes the
calling context from ``symbols.caller``, matching the official implementation.

中文说明
--------
日志服务 ``ctx.logger``。它是一个「可调用服务」：``ctx.logger('name')`` 返回一个
绑定到「调用方 fiber」的 :class:`Logger`，因此日志能自动带上来源信息。

分工：

* :class:`LoggerService` —— 导出器（exporter）管理与消息分发。默认注册一个
  「写入内存缓冲区」的导出器（``self.buffer``，上限 1000 条）并开启彩色输出；
  调用 ``ctx.logger.exporter({...})`` 可添加自定义导出器（写文件、上报等）。
* :class:`Logger` —— 单条日志的构造与格式化。提供 ``error`` / ``warn`` /
  ``info`` / ``debug`` 四个级别，支持 ``%s`` 风格的占位符（``%o`` 输出对象、
  ``%C`` 彩色名字等，见 :data:`default_formatters`）。
* 级别过滤：导出器可通过 ``levels`` 按「日志名」或 ``default`` 指定阈值，
  低于阈值的消息直接丢弃。

日志名解析顺序：显式参数 → 拦截层（``intercept``）中的 ``logger.name`` →
调用方 fiber 名转连字符风格（例如 ``my_plugin`` → ``my-plugin``）。
"""

from __future__ import annotations

import json
import re
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict, TypeGuard, cast

from .utils import (
    Tracker,
    get_symbol,
    register_protocol,
    set_symbol,
    symbols,
)

if TYPE_CHECKING:
    from .context import Context
    from .fiber import Fiber


#: 日志级别名（对应 ``%s`` 之外的方法名）
LoggerType = Literal["error", "info", "warn", "debug"]


class Formatter(Protocol):
    """占位符格式化器：``(值, exporter, message) -> 任意可打印值``。"""

    def __call__(
        self, value: Any, exporter: Exporter, message: Message
    ) -> Any: ...


class Exporter(TypedDict, total=False):
    """导出器配置。

    字段:
        colors: 着色档位（``False`` 关闭；1/2/3 越高装饰越多）。
        maxLength: 单行最大长度，超出截断并追加 ``...``（默认 10240）。
        levels: 按日志名（或 ``default``）指定最低级别，低于即丢弃。
        formatters: 自定义占位符格式化器（键为占位符字母）。
        export: 实际写出函数，接收 :class:`Message`。
    """

    colors: int | Literal[False]
    maxLength: int
    levels: dict[str, int]
    formatters: dict[str, Formatter]
    export: Callable[[Message], None]


class LoggerIntercept(TypedDict, total=False):
    """拦截层中的日志配置：``name`` 覆写日志名，``level`` 覆写最低级别。"""

    name: str
    level: int


class LoggerLevel(IntEnum):
    """日志级别（数值越大越详细）；只输出「级别 <= logger.level」的消息。"""

    ERROR = 0
    WARN = 1
    INFO = 2
    DEBUG = 3


#: 16 色 ANSI 调色板（低级别日志用）
c16 = [6, 2, 3, 4, 5, 1]
#: 256 色 ANSI 调色板（INFO 及以上用，颜色更丰富）
c256 = [
    20, 21, 26, 27, 32, 33, 38, 39, 40, 41, 42, 43, 44, 45, 56, 57, 62,
    63, 68, 69, 74, 75, 76, 77, 78, 79, 80, 81, 92, 93, 98, 99, 112, 113,
    129, 134, 135, 148, 149, 160, 161, 162, 163, 164, 165, 166, 167, 168,
    169, 170, 171, 172, 173, 178, 179, 184, 185, 196, 197, 198, 199, 200,
    201, 202, 203, 204, 205, 206, 207, 208, 209, 214, 215, 220, 221,
]


def _s(value: Any, exporter: Exporter | None = None, message: Message | None = None) -> str:
    """``%s``：转字符串。"""
    return str(value)


def _d(value: Any, exporter: Exporter | None = None, message: Message | None = None) -> int:
    """``%d``：转整数。"""
    return int(float(value))


def _i(value: Any, exporter: Exporter | None = None, message: Message | None = None) -> int:
    """``%i``：转整数。"""
    return int(float(value))


def _f(value: Any, exporter: Exporter | None = None, message: Message | None = None) -> float:
    """``%f``：转浮点数。"""
    return float(value)


def _o(value: Any, exporter: Exporter | None = None, message: Message | None = None) -> str:
    """``%o``：JSON 序列化（不可序列化的对象回退到 ``repr``）。"""
    return json.dumps(value, ensure_ascii=False, default=repr)


def _c(value: Any, exporter: Exporter | None = None, message: Message | None = None) -> str:
    """``%c``：CSS 样式占位符（浏览器控制台专用），终端下输出空串。"""
    return ""


def _C(value: Any, exporter: Exporter | None = None, message: Message | None = None) -> str:
    """``%C``：给内容着色（颜色由日志名哈希决定）。"""
    if message is None or exporter is None:
        return str(value)
    return Logger.color(exporter, Logger.code(message.name, _colors(exporter)), value)


#: 默认占位符格式化器表（键为占位符字母）
default_formatters: dict[str, Formatter] = {
    "s": _s,
    "d": _d,
    "i": _i,
    "f": _f,
    "o": _o,
    "O": _o,
    "c": _c,
    "C": _C,
}


def _colors(exporter: Exporter | None) -> int | Literal[False]:
    """读取导出器的着色配置（缺省关闭）。"""
    return cast(int | Literal[False], _option(exporter, "colors", False))


@dataclass
class Message:
    """Port of TS ``interface Message``.

    一条日志消息。

    字段:
        sn: 全局自增序号（同一进程内唯一，便于排序）。
        ts: 毫秒时间戳。
        name: 日志名（决定颜色与级别过滤）。
        type: 级别名（``error``/``warn``/``info``/``debug``）。
        level: 级别数值。
        args: 原始参数（格式化用）。
        fiber: 产生日志的 fiber（弱引用，避免阻碍回收）。
        meta: 附加元数据（不含 fiber）。
    """

    sn: int
    ts: int
    name: str
    type: LoggerType
    level: int
    args: list[Any]
    fiber: weakref.ReferenceType[Fiber] | Fiber | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoggerOptions:
    """构造 :class:`Logger` 的选项：名字、元数据、最低级别。"""

    name: str
    meta: dict[str, Any] = field(default_factory=dict)
    level: int | None = None


class _AggregateErrorLike(Protocol):
    """形如聚合异常的对象（带 ``errors`` 列表）。"""

    errors: list[BaseException]


def is_aggregate_error(error: Any) -> TypeGuard[_AggregateErrorLike]:
    """Port of ``isAggregateError``.

    判断是否为「聚合异常」：``BaseException`` 且带 ``errors`` 列表/元组
    （:class:`~common.cordis.events.EventDispatchError` 即属此类）。
    """
    return isinstance(error, BaseException) and isinstance(getattr(error, "errors", None), (list, tuple))


class Logger:
    """Port of the official ``Logger`` class.

    单个日志记录器（由 ``ctx.logger(name)`` 产生）。除四个级别方法外，
    还提供两个静态工具：:meth:`format`（格式化）与 :meth:`color` / :meth:`code`
    （着色）。
    """

    name: str
    level: int
    meta: dict[str, Any]

    def __init__(self, options: LoggerOptions, service: LoggerService) -> None:
        self.name = options.name
        self.level = options.level if options.level is not None else LoggerLevel.INFO
        self.meta = options.meta
        self._service = service

    # -- formatting helpers —— 格式化与着色 ----------------------------------
    @staticmethod
    def color(exporter: Exporter, code: int, value: Any, decoration: str = "") -> str:
        """Port of ``Logger.color``.

        用 ANSI 转义序列给文本着色：

        * 未启用颜色 → 原样返回；
        * ``code < 8`` 用 16 色写法（``\\x1b[3Xm``）；
        * 其余用 256 色写法（``\\x1b[38;5;Nm``）；
        * 颜色档位 >= 2 时才附加 ``decoration``（如加粗/下划线）。
        """
        colors = _option(exporter, "colors", False)
        if not colors:
            return str(value)
        color_code = str(code) if code < 8 else f"8;5;{code}"
        tail = decoration if colors >= 2 else ""
        return f"\x1b[3{color_code}{tail}m{value}\x1b[0m"

    @staticmethod
    def code(name: str, level: int | Literal[False] | None = None) -> int:
        """Port of ``Logger.code``: stable hash into the color palette.

        用稳定哈希把日志名映射到调色板中的一个颜色：

        * 哈希算法与官方一致（``h * 31 + 字符码 + 13``，32 位环绕），因此颜色
          在同一日志名下保持稳定；
        * 级别 >= INFO 用 256 色调色板，否则用 16 色；
        * 关闭颜色时返回 0。
        """
        hash_value = 0
        for char in name:
            hash_value = ((hash_value << 3) - hash_value) + ord(char) + 13
            hash_value &= 0xFFFFFFFF
        if hash_value >= 0x80000000:
            # 取有符号 32 位整数，与 JS 结果一致
            hash_value -= 0x100000000
        if not level:
            colors: list[int] = []
        elif level >= 2:
            colors = c256
        else:
            colors = c16
        if not colors:
            return 0
        return colors[abs(hash_value) % len(colors)]

    @staticmethod
    def format(exporter: Exporter, message: Message) -> str:
        """Port of ``Logger.format``.

        把消息格式化为最终文本：

        1. 若首个参数是异常，先渲染其 traceback（并插入 ``%s`` 占位符）；
        2. 否则若首个参数不是字符串，插入 ``%o`` 以便对象也能打印；
        3. 逐个替换 ``%x`` 占位符（``%%`` 转义为 ``%``），未知占位符原样保留；
        4. 剩余参数依次追加（对象形态的参数用 ``%o`` 渲染）；
        5. 按 ``maxLength`` 逐行截断超长内容。
        """
        args = list(message.args)
        if args and isinstance(args[0], BaseException):
            error = args[0]
            args[0] = "".join(_format_traceback(error))
            args.insert(0, "%s")
        elif not args or not isinstance(args[0], str):
            args.insert(0, "%o")

        fmt: str = args.pop(0) if args else ""

        def replace(match: re.Match[str]) -> str:
            char = match.group(1)
            if match.group(0) == "%%":
                return "%"
            # 先查导出器自定义格式化器，再回退到默认表
            formatter = (_option(exporter, "formatters", {}) or {}).get(char)
            if formatter is None:
                formatter = default_formatters.get(char)
            if callable(formatter):
                value = args.pop(0) if args else None
                return str(formatter(value, exporter, message))
            return match.group(0)

        fmt = re.sub(r"%([a-zA-Z%])", replace, fmt)

        # 未匹配到占位符的剩余参数直接追加（对象用 %o 渲染）
        o_formatter = (_option(exporter, "formatters", {}) or {}).get("o") or _o
        for arg in args:
            if isinstance(arg, (dict, list, tuple)) and arg:
                arg = o_formatter(arg, exporter, message)
            fmt += " " + str(arg)

        # 逐行限长，避免日志被超长内容淹没
        max_length = _option(exporter, "maxLength", 10240)
        lines = re.split(r"\r?\n", fmt)
        return "\n".join(
            line[:max_length] + ("..." if len(line) > max_length else "") for line in lines
        )

    # -- logging methods —— 四个级别方法 -------------------------------------
    def error(self, *args: Any) -> None:
        """记录错误级日志。"""
        self._method("error", LoggerLevel.ERROR)(*args)

    def warn(self, *args: Any) -> None:
        """记录警告级日志。"""
        self._method("warn", LoggerLevel.WARN)(*args)

    def info(self, *args: Any) -> None:
        """记录信息级日志。"""
        self._method("info", LoggerLevel.INFO)(*args)

    def debug(self, *args: Any) -> None:
        """记录调试级日志。"""
        self._method("debug", LoggerLevel.DEBUG)(*args)

    def _method(self, type_: LoggerType, level: int) -> Callable[..., None]:
        """Port of ``Logger._method``: unwrap Error causes, then export.

        生成具体的记录函数：

        * 单个异常参数且带 ``__cause__`` → 先记录原因（更底层的信息更有用）；
        * 聚合异常 → 逐个记录其 ``errors``（并提前返回）；
        * 否则构造 :class:`Message` 并分发给所有导出器，按各自的 ``levels``
          配置做过滤（``default`` 兜底，再回退到 logger 自身级别）。
        """

        def method(*args: Any) -> None:
            if len(args) == 1 and isinstance(args[0], BaseException):
                cause = getattr(args[0], "__cause__", None)
                if cause is not None:
                    self._method(type_, level)(cause)
                if is_aggregate_error(args[0]):
                    for error in args[0].errors:
                        self._method(type_, level)(error)
                    return

            service = self._service._root
            service._sn_message += 1
            message = Message(
                sn=service._sn_message,
                ts=int(time.time() * 1000),
                name=self.name,
                type=type_,
                level=int(level),
                args=list(args),
                fiber=self.meta.get("fiber"),
                meta={k: v for k, v in self.meta.items() if k != "fiber"},
            )
            for exporter in list(service.exporters.values()):
                levels = _option(exporter, "levels", {}) or {}
                # 优先级：按日志名的阈值 > default > logger 自身级别
                target_level = levels.get(
                    self.name,
                    levels.get("default", self.level if self.level is not None else LoggerLevel.INFO),
                )
                if target_level is not None and int(target_level) < int(level):
                    continue
                export = _option(exporter, "export", None)
                if callable(export):
                    export(message)

        return method


class LoggerService:
    """Port of the official ``LoggerService``.

    Calling it produces a :class:`Logger`; the call is routed through
    ``symbols.invoke`` so the calling context (``symbols.caller``) can be read.

    日志服务（``ctx.logger``）：管理导出器与缓冲区，并作为「可调用服务」生产
    :class:`Logger`。调用方上下文通过 ``symbols.caller`` 获取，因此
    ``ctx.logger(...)`` 在哪个插件里调用，日志就归属哪个插件。
    """

    #: 缓冲区上限（消息条数），由 ``bufferSize`` 读写
    buffer_size = 1000

    @property
    def bufferSize(self) -> int:
        """缓冲区容量（官方属性名的 camelCase 版本）。"""
        return self._root.buffer_size

    @bufferSize.setter
    def bufferSize(self, value: int) -> None:
        self._root.buffer_size = int(value)

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._root = self
        set_symbol(self, symbols.tracker, Tracker(property="ctx", no_shadow=True))
        #: 内存中的日志缓冲区（默认导出器写入这里，便于测试与回溯）
        self.buffer: list[Message] = []
        self._sn_message = 0
        self._sn_exporter = 0
        #: 导出器表：自增序号 -> 配置
        self.exporters: dict[int, Exporter] = {}
        # 默认导出器：3 档着色 + 写入缓冲区
        self.exporter(
            {
                "colors": 3,
                "export": self._buffer_export,
            },
            raw=True,
        )

    # -- callable service —— 可调用服务 --------------------------------------
    def __call__(self, name: str | None = None, caller: Any = None) -> Logger:
        """``ctx.logger('name')``：创建绑定到调用方的 Logger。"""
        return self._invoke(name, caller)

    def __invoke__(self, name: str | None = None) -> Logger:
        """Python counterpart of ``[symbols.invoke]``.

        对应官方的符号调用入口：调用方上下文从 ``symbols.caller`` 读取
        （由追踪代理在调用时注入）。
        """
        caller = get_symbol(self, symbols.caller)
        return self._invoke(name, caller)

    def _invoke(self, name: str | None, caller: Any = None) -> Logger:
        """解析配置与日志名，构造 :class:`Logger`。

        日志名优先级：显式 ``name`` → 拦截层配置的 ``logger.name`` →
        调用方 fiber 名（下划线转连字符）。
        """
        config = self._resolve_config()
        fiber = (caller or self.ctx).fiber
        resolved = name or config.get("name") or _hyphenate(fiber.name)
        return Logger(
            LoggerOptions(
                name=resolved,
                level=config.get("level"),
                meta={"fiber": _weak_fiber(fiber)},
            ),
            self._root,
        )

    def _resolve_config(self) -> LoggerIntercept:
        """Walk the intercept chain and merge ``logger`` configs.

        沿拦截层（``intercept`` 原型链）收集 ``logger`` 配置并合并：越靠近当前
        上下文的层优先级越高（因此用 ``insert(0, ...)``）。
        """
        intercept = self.ctx.__get_symbol__(symbols.intercept, {})
        configs: list[Any] = []
        layers = intercept.chain() if hasattr(intercept, "chain") else [intercept]
        for layer in layers:
            if "logger" in layer:
                # 只取本层「自有」的 logger 配置，避免重复收集原型链上的值
                if hasattr(layer, "has_own"):
                    if layer.has_own("logger"):
                        configs.insert(0, layer["logger"])
                else:
                    configs.insert(0, dict.get(layer, "logger"))
        result: LoggerIntercept = {}
        for config in configs:
            if isinstance(config, dict):
                result.update(cast(LoggerIntercept, config))
        return result

    # -- level shortcuts -----------------------------------------------------
    def error(self, *args: Any) -> None:
        """``ctx.logger.error(...)``：直接用默认日志名记录错误。"""
        self().error(*args)

    def warn(self, *args: Any) -> None:
        """``ctx.logger.warn(...)``：直接用默认日志名记录警告。"""
        self().warn(*args)

    def info(self, *args: Any) -> None:
        """``ctx.logger.info(...)``：直接用默认日志名记录信息。"""
        self().info(*args)

    def debug(self, *args: Any) -> None:
        """``ctx.logger.debug(...)``：直接用默认日志名记录调试信息。"""
        self().debug(*args)

    # -- exporters —— 导出器 -------------------------------------------------
    def exporter(self, exporter: Exporter, raw: bool = False) -> Callable[[], Any]:
        """Port of ``exporter``: register inside an effect.

        注册导出器并返回注销函数。

        参数:
            exporter: 导出器配置（含 ``export`` 回调等）。
            raw: 为 ``True`` 时直接登记（供构造期的默认导出器使用，不创建 effect）；
                为 ``False`` 时包成 effect，随 fiber 卸载自动注销。
        """
        if raw:
            self._sn_exporter += 1
            self.exporters[self._sn_exporter] = exporter
            return lambda: self.exporters.pop(self._sn_exporter, None)

        def execute() -> Any:
            self._root._sn_exporter += 1
            exporter_id = self._root._sn_exporter
            self._root.exporters[exporter_id] = exporter

            def dispose() -> None:
                self._root.exporters.pop(exporter_id, None)

            return dispose

        return self.ctx.fiber.effect(execute, "ctx.logger.exporter()")

    def _buffer_export(self, message: Message) -> None:
        """Default exporter: ring buffer with the official overflow handling.

        默认导出器：写入内存环形缓冲区。溢出处理与官方一致——只多出 1 条时弹一条，
        多出多条时批量裁剪，避免高频日志时反复移动列表。
        """
        self.buffer.append(message)
        overflow = len(self.buffer) - self.buffer_size
        if overflow == 1:
            self.buffer.pop(0)
        elif overflow > 1:
            del self.buffer[:overflow]

    # -- for-compatibility —— 上下文派生 -------------------------------------
    def for_context(self, ctx: Context) -> LoggerService:
        """派生出面向 ``ctx`` 的同构服务（共享缓冲区与导出器表）。"""
        service = object.__new__(type(self))
        service.ctx = ctx
        service._root = self._root
        service.buffer = self._root.buffer
        service._sn_message = self._root._sn_message
        service._sn_exporter = self._root._sn_exporter
        service.exporters = self._root.exporters
        set_symbol(service, symbols.tracker, Tracker(property="ctx", no_shadow=True))
        return service

    def __get_symbol__(self, symbol: Any, default: Any = None) -> Any:
        # 官方用符号键挂 ``[symbols.invoke]``；这里映射到 __invoke__ 上
        if symbol == symbols.invoke:
            return self.__invoke__
        return getattr(self, "_cordis_" + symbol.name.replace(".", "_"), default)

    def __repr__(self) -> str:
        return f"<LoggerService buffer={len(self.buffer)}>"


def _hyphenate(value: str) -> str:
    """Port of cosmokit ``hyphenate``.

    把驼峰/下划线风格的标识符转成连字符风格（``MyPlugin`` → ``my-plugin``），
    用作没有显式指定名称时的默认日志名。
    """
    result: list[str] = []
    for index, char in enumerate(value):
        if char.isupper() and index:
            result.append("-")
        result.append(char.lower())
    return "".join(result)


def _format_traceback(error: BaseException) -> list[str]:
    """把异常渲染成 traceback 文本行（用于异常作为首个日志参数时）。"""
    import traceback

    return traceback.format_exception(type(error), error, error.__traceback__)


def _weak_fiber(fiber: Fiber | None) -> weakref.ReferenceType[Fiber] | Fiber | None:
    """尽可能把 fiber 保存为弱引用，避免日志元数据阻碍 fiber 回收。"""
    if fiber is None:
        return None
    try:
        return weakref.ref(fiber)
    except TypeError:
        return fiber


def _option(exporter: Exporter | None, name: str, default: Any) -> Any:
    """读取导出器配置项；兼容字典与对象两种形态，缺失时返回 ``default``。"""
    if exporter is None:
        return default
    if isinstance(exporter, dict):
        return exporter.get(name, default)
    return getattr(exporter, name, default)


# make `logger` a callable service through the shared invoke protocol
# 让 ``ctx.logger(...)`` 走统一的可调用协议（对应官方 ``[symbols.invoke]``）
register_protocol(symbols.invoke, "__invoke__")
