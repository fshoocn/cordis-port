"""EventsService: event dispatch (port of ``core/src/events.ts``).

Supports the five dispatch modes (``emit``, ``parallel``, ``serial``, ``bail``,
``waterfall``), the ``thisArg`` shorthand, the ``Context.filter`` protocol, the
``internal/*`` hooks and the deprecated ``dispatch`` API.

中文说明
--------
事件总线 ``ctx.events``。支持五种派发模式（对应官方同名方法）：

============  ==================================================
方法           行为
============  ==================================================
``emit``       依次调用所有监听器，忽略返回值（同步）
``parallel``   并发执行所有监听器，任一失败则汇总为聚合异常
``serial``     依次执行，遇到「非空返回值」立即中止并返回它
``bail``       同 ``serial``，但全程同步（不 await）
``waterfall``  链式传递：每个监听器收到 ``next`` 回调，可决定是否继续
============  ==================================================

三个约定：

* **thisArg 简写**：``ctx.emit(ctx, 'event', ...)`` 中第一个参数若是对象/可调用
  对象，则被识别为 ``thisArg``（供 ``Context.filter`` 协议做隔离过滤）；
* **过滤协议**：``thisArg[Context.filter]`` 返回假值时，跳过不在同一隔离层的监听器；
* **内部钩子**：``internal/*`` 事件用于框架自身扩展，例如 ``internal/dispatch``
  在每次派发前触发，``internal/listener`` 可截获注册行为（fiber 私有钩子即由此实现）。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from .utils import DisposableList, Tracker, _Symbol, get_symbol, set_symbol, symbols

if TYPE_CHECKING:
    from .context import Context
    from .fiber import Fiber


def is_bailed(value: Any) -> bool:
    """Port of ``isBailed``: ``value !== null && value !== false && value !== undefined``.

    判断监听器返回值是否算「有效结果」（用于 ``serial`` / ``bail`` 的提前中止）：
    Python 中 ``None`` 与 ``False`` 视为「未处理」，其余值（含 0、空字符串）都算命中。
    """
    return value is not None and value is not False


@dataclass
class EventOptions:
    """监听器注册选项。

    字段:
        prepend: 插到监听器列表最前面（后注册者先执行）。
        global_: 全局监听器，不受 ``Context.filter`` 隔离过滤影响。
    """

    prepend: bool = False
    global_: bool = False


#: 事件名：字符串或符号（``internal/*`` 用字符串，框架内部扩展点建议用符号避免冲突）
EventName = str | _Symbol
#: ``on`` 的宽松选项形式：布尔（等价 ``prepend``）、选项对象、字典或 ``None``
EventOptionsInput = bool | EventOptions | Mapping[str, bool] | None


@dataclass
class Hook:
    """一条已注册的监听器记录。

    字段:
        ctx: 注册该监听器时所在的上下文（用于隔离过滤与「谁注册的」溯源）。
        callback: 监听器本体。
        prepend: 是否前置插入。
        global_: 是否全局（跳过过滤）。
    """

    ctx: Context
    callback: Callable[..., Any]
    prepend: bool = False
    global_: bool = False


class EventDispatchError(Exception):
    """Aggregate error raised by ``parallel`` (port of ``AggregateError``).

    ``parallel`` 中多个监听器失败时，把它们的异常收集进 ``errors`` 一并抛出。
    """

    def __init__(self, errors: list[BaseException]) -> None:
        self.errors = errors
        super().__init__(
            "; ".join(str(error) for error in errors)
            or f"{len(errors)} event callback(s) failed"
        )


@dataclass
class EventsService:
    """Port of the official ``EventsService``.

    事件服务。所有上下文共享同一份 ``_hooks`` 表（``for_context`` 只是换一个
    ``ctx`` 视角），因此子上下文注册的监听器对整棵树都可见，隔离交由
    过滤协议 + ``global_`` 标记控制。
    """

    ctx: Context
    _root: EventsService = field(init=False, repr=False)
    #: 事件名 -> 监听器列表
    _hooks: dict[EventName, list[Hook]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._root = self
        set_symbol(self, symbols.tracker, Tracker(property="ctx", no_shadow=True))
        self._install_internal_hooks()

    def for_context(self, ctx: Context) -> EventsService:
        """派生出面向 ``ctx`` 的同构服务（共享 ``_hooks``）。"""
        service = object.__new__(type(self))
        service.ctx = ctx
        service._root = self._root
        service._hooks = self._hooks
        set_symbol(service, symbols.tracker, Tracker(property="ctx", no_shadow=True))
        return service

    # -- constructor hooks —— 构造期安装的内部钩子 ---------------------------
    def _install_internal_hooks(self) -> None:
        """Port of the two ``this.on(...)`` calls in the official constructor.

        安装两个内部监听器：

        1. ``internal/listener``（普通监听）—— 当注册的事件是 ``internal/update``
           且未标记 ``global_`` 时，把监听器收进 ``fiber._hooks``（fiber 私有钩子），
           而不是进全局 ``_hooks``，从而避免影响其他插件；
        2. ``internal/update``（前置、全局）—— 依次执行每个 fiber 的私有
           ``internal/update`` 钩子，全部走完才调用 ``next_hook``（默认更新逻辑）。
        """

        def on_listener(
            target: Context,
            name: EventName,
            listener: Callable[..., Any],
            options: EventOptions,
        ) -> Any:
            """``internal/listener``: private ``internal/update`` hooks.

            把 ``internal/update`` 的注册改为「挂到目标 fiber 私有钩子表」，
            返回注销函数；其它事件返回 ``None`` 表示不拦截。
            """
            if name == "internal/update" and not options.global_:
                hooks = target.fiber._hooks.setdefault("internal/update", DisposableList())
                if options.prepend:
                    return hooks.unshift(listener)
                return hooks.push(listener)
            return None

        def on_update(
            target: Fiber,
            config: Any,
            no_save: bool,
            next_hook: Callable[..., Any],
        ) -> Any:
            """``internal/update``: run every private hook, then ``next``.

            把 fiber 私有钩子串成责任链：每个钩子都能选择先做自己的事再调 ``next``，
            或用 ``next`` 跳过后续逻辑（支持带参覆盖配置的钩子覆盖）。
            """
            cbs = list(target._hooks.get("internal/update", []))

            def _next() -> Any:
                callback = cbs.pop(0) if cbs else next_hook
                return callback(config, no_save, _next)

            return _next()

        # 标记这两个回调需要 ``thisArg`` 作为第一个参数（官方 ``this`` 语义）
        on_listener._cordis_uses_this_arg = True  # type: ignore[attr-defined]
        on_update._cordis_uses_this_arg = True  # type: ignore[attr-defined]

        self._hooks.setdefault("internal/listener", []).append(
            Hook(ctx=self.ctx, callback=on_listener)
        )
        self._hooks.setdefault("internal/update", []).insert(
            0, Hook(ctx=self.ctx, callback=on_update, prepend=True, global_=True)
        )

    # -- resolution —— 监听器解析 --------------------------------------------
    def _resolve(
        self, mode: str, args: list[Any]
    ) -> tuple[Any, list[Callable[..., Any]]]:
        """Port of ``_resolve``: pop ``thisArg``/name, honour filters and hooks.

        从实参列表中解析出事件名与「待调用回调列表」：

        1. 若首个实参是对象/可调用对象，视作 ``thisArg`` 弹出；
        2. 弹出事件名（非字符串/符号则视为无效，返回空列表）；
        3. 派发 ``internal/dispatch``（对 ``internal/*`` 事件自身不派发，防止递归）；
        4. 遍历该事件的监听器，按 ``Context.filter`` 协议过滤隔离层；
        5. 需要 ``thisArg`` 的回调（``_cordis_uses_this_arg``）绑定 ``thisArg``。
        """
        this_arg: Any = None
        if args and _is_this_arg(args[0]):
            this_arg = args.pop(0)
        name = args.pop(0) if args else None
        if not isinstance(name, (str, _Symbol)):
            return this_arg, []

        hooks = self._hooks.get("internal/dispatch")
        if hooks and (not isinstance(name, str) or not name.startswith("internal/")):
            # 通知所有 ``internal/dispatch`` 监听器（调试/追踪用途）；
            # ``internal/*`` 自身不再触发，避免无限递归
            self.emit("internal/dispatch", mode, name, args, this_arg)

        filter_callback = _resolve_filter(this_arg)
        callbacks: list[Callable[..., Any]] = []
        for hook in list(self._hooks.get(name, ())):
            if not hook.global_ and filter_callback is not None and not filter_callback(hook.ctx):
                continue
            callback = hook.callback
            if getattr(callback, "_cordis_uses_this_arg", False):
                callback = _bind_this_arg(callback, this_arg)
            callbacks.append(callback)
        return this_arg, callbacks

    # -- dispatch modes —— 五种派发模式 --------------------------------------
    async def parallel(self, *args: Any) -> None:
        """Port of ``parallel``: run everything, raise an aggregate error.

        并发执行所有监听器：全部等待完成，若存在异常则统一抛
        :class:`EventDispatchError`（``errors`` 保留原始异常列表）。
        """
        pending = list(args)
        this_arg, callbacks = self._resolve("emit", pending)
        results = await asyncio.gather(
            *(_invoke(callback, this_arg, pending) for callback in callbacks),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise EventDispatchError(errors)

    def emit(self, *args: Any) -> None:
        """``emit``：同步依次调用所有监听器，忽略返回值（日志/通知型事件）。"""
        pending = list(args)
        this_arg, callbacks = self._resolve("emit", pending)
        for callback in callbacks:
            _apply(callback, this_arg, pending)

    async def serial(self, *args: Any) -> Any:
        """``serial``：依次调用，遇到有效返回值即中止并返回它（可 await）。

        与 ``emit`` 的区别是「谁先返回非 None/False 就听谁的」，用于多插件协商、
        取第一个可用实现等场景。
        """
        pending = list(args)
        this_arg, callbacks = self._resolve("serial", pending)
        for callback in callbacks:
            result = _apply(callback, this_arg, pending)
            if inspect.isawaitable(result):
                result = await result
            if is_bailed(result):
                return result
        return None

    def bail(self, *args: Any) -> Any:
        """``bail``：``serial`` 的同步版本（不 await 返回值）。"""
        pending = list(args)
        this_arg, callbacks = self._resolve("bail", pending)
        for callback in callbacks:
            result = _apply(callback, this_arg, pending)
            if is_bailed(result):
                return result
        return None

    def waterfall(self, *args: Any) -> Any:
        """Port of ``waterfall``: pass a ``next`` callback through the chain.

        链式传递：每个监听器都会额外收到一个 ``next`` 回调，调用它才继续到下一个
        监听器；链条末尾调用初始的 ``inner``（最后一个实参，通常是默认实现）。
        这让监听器可以「环绕」目标逻辑（前置准备 + 后置处理）。
        """
        pending = list(args)
        this_arg, callbacks = self._resolve("waterfall", pending)
        inner = pending.pop() if pending else None
        if not callable(inner):
            inner = lambda *a, **k: None

        index = 0

        def dispatch() -> Any:
            nonlocal index
            if index >= len(callbacks):
                # the official `inner()` takes no arguments: callbacks
                # communicate through the `next(value)` chain instead
                # 链条走完：调用默认实现（不传参，值通过 next 链传递）
                return inner()
            callback = callbacks[index]
            index += 1
            called = False

            def next_callback(*_ignored: Any) -> Any:
                # 官方语义：同一个监听器内重复调用 next 属于编码错误
                nonlocal called
                if called:
                    raise RuntimeError("next() called multiple times")
                called = True
                return dispatch()

            return _apply(callback, this_arg, tuple(pending) + (next_callback,))

        return dispatch()

    def dispatch(self, mode: str, args: list[Any]) -> list[Any]:
        """Deprecated ``dispatch`` (port), kept for compatibility.

        已废弃：仅解析并返回「绑定后的回调列表」，交由调用方自行执行。
        """
        this_arg, callbacks = self._resolve(mode, list(args))
        return [_bind(callback, this_arg) for callback in callbacks]

    # -- registration —— 注册与注销 ------------------------------------------
    def register(
        self,
        label: str,
        name: EventName,
        callback: Callable[..., Any],
        options: EventOptions,
    ) -> Callable[[], Any]:
        """把监听器登记进 ``_hooks``，并包装成 fiber effect。

        effect 生命周期与当前 fiber 绑定：fiber 卸载时监听器自动注销。
        ``execute`` 返回的是注销函数，因此 effect 销毁即执行注销。
        """

        def execute() -> Callable[[], None]:
            hooks = self._hooks.setdefault(name, [])
            hook = Hook(ctx=self.ctx, callback=callback, prepend=options.prepend, global_=options.global_)
            if options.prepend:
                hooks.insert(0, hook)
            else:
                hooks.append(hook)

            def dispose() -> None:
                self.unregister(name, callback)

            return dispose

        return self.ctx.fiber.effect(execute, label)

    def unregister(self, name: EventName, callback: Callable[..., Any]) -> bool | None:
        """按回调对象移除监听器。

        返回 ``True`` 表示移除成功；``False`` 表示该事件无监听器；
        ``None`` 表示事件存在但未找到该回调（官方返回值语义）。
        """
        hooks = self._hooks.get(name)
        if not hooks:
            return False
        for index, hook in enumerate(hooks):
            if hook.callback is callback:
                hooks.pop(index)
                if not hooks:
                    self._hooks.pop(name, None)
                return True
        return None

    def on(
        self,
        name: EventName,
        listener: Callable[..., Any],
        options: EventOptionsInput = None,
    ) -> Callable[[], Any]:
        """Port of ``on``.

        注册监听器并返回注销函数。流程：

        1. 归一化选项；
        2. 校验当前 fiber 处于激活状态；
        3. 用 ``reflect.bind`` 包装监听器（实参/返回值都带上下文追踪）；
        4. 先尝试 ``internal/listener`` 截获（fiber 私有钩子走这条路）；
        5. 否则正常登记。
        """
        normalized = _normalize_options(options)
        self.ctx.fiber.assert_active()
        bound = self.ctx.reflect.bind(listener)

        # special events: `internal/listener` may consume the registration
        # 特殊事件：``internal/listener`` 监听器可以「接管」本次注册
        result = self.bail(self.ctx, "internal/listener", name, bound, normalized)
        if result:
            return result

        label = f"ctx.on({name!r})"
        return self.register(label, name, bound, normalized)

    def once(
        self,
        name: EventName,
        listener: Callable[..., Any],
        options: EventOptionsInput = None,
    ) -> Callable[[], Any]:
        """Port of ``once``.

        只触发一次的监听器：首次触发时先注销自身，再调用原监听器。
        返回的仍是「主动注销」函数（用于在触发前取消监听）。
        """
        dispose_holder: list[Callable[[], Any]] = []
        this_ctx = self.ctx

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 先注销（dispose 幂等，重复调用安全），再执行监听器
            if dispose_holder:
                dispose_holder[0]()
            return _apply(listener, this_ctx, args, kwargs)

        dispose_holder.append(self.on(name, wrapper, options))
        return dispose_holder[0]


# ---------------------------------------------------------------------------
# helpers —— 辅助函数
# ---------------------------------------------------------------------------


def _is_ctx(value: Any) -> bool:
    return bool(getattr(value, "is_context", False))


def _is_this_arg(value: Any) -> bool:
    """Port of ``typeof args[0] === 'object' || typeof args[0] === 'function'``.

    JS decides purely by ``typeof``, which has no Python counterpart for numbers
    and booleans, so this only accepts values that cannot be an event name:
    contexts, objects carrying the filter protocol, or plain callables used as
    ``thisArg`` when the event name is a symbol.

    判定首个实参是否为 ``thisArg``：凡是「不可能是事件名」的值（数字、布尔、
    字符串等标量都不是）才接受，因此排除标量后剩余对象/函数即视为 ``thisArg``。
    """
    return value is not None and not isinstance(
        value,
        (str, bytes, int, float, bool, complex),
    )


def _resolve_filter(this_arg: Any) -> Callable[[Context], bool] | None:
    """Port of ``thisArg?.[Context.filter]``.

    取 ``thisArg`` 上的过滤协议（``Context.filter`` 符号对应的 ``__filter__``）；
    没有则返回 ``None``（表示不做隔离过滤）。
    """
    if this_arg is None:
        return None
    filter_callback = get_symbol(this_arg, symbols.filter)
    if callable(filter_callback):
        return cast(Callable[..., bool], filter_callback)
    return None


def _bind_this_arg(callback: Callable[..., Any], this_arg: Any) -> Callable[..., Any]:
    """把 ``thisArg`` 作为第一个实参绑定到回调（模拟 JS 的 ``this``）。"""

    def bound(*args: Any, **kwargs: Any) -> Any:
        return callback(this_arg, *args, **kwargs)

    return bound


def _normalize_options(options: EventOptionsInput) -> EventOptions:
    """把宽松选项统一成 :class:`EventOptions`。

    支持四种写法：``True/False``（等价 ``prepend``）、``EventOptions``、
    ``{"prepend": ..., "global": ...}`` 字典、以及带同名属性的普通对象。
    """
    if isinstance(options, EventOptions):
        return options
    if isinstance(options, bool):
        return EventOptions(prepend=options)
    if options is None:
        return EventOptions()
    if isinstance(options, dict):
        return EventOptions(
            prepend=bool(options.get("prepend", False)),
            global_=bool(options.get("global", False)),
        )
    return EventOptions(prepend=bool(getattr(options, "prepend", False)),
                        global_=bool(getattr(options, "global_", False)))


def _apply(
    callback: Callable[..., Any],
    this_arg: Any,
    args: Iterable[Any],
    kwargs: Mapping[str, Any] | None = None,
) -> Any:
    """Port of ``Reflect.apply(callback, thisArg, args)``.

    调用回调。注意：Python 没有动态 ``this``，``thisArg`` 只在需要时由
    :func:`_bind_this_arg` 预绑定，这里不再重复传递。
    """
    if this_arg is None:
        return callback(*tuple(args), **(kwargs or {}))
    return callback(*tuple(args), **(kwargs or {}))


def _bind(callback: Callable[..., Any], this_arg: Any) -> Callable[..., Any]:
    """绑定 ``thisArg`` 的轻量版本（``this_arg`` 为 ``None`` 时直接返回原回调）。"""
    if this_arg is None:
        return callback

    def bound(*args: Any, **kwargs: Any) -> Any:
        return callback(*args, **kwargs)

    return bound


async def _invoke(callback: Callable[..., Any], this_arg: Any, args: Iterable[Any]) -> Any:
    """调用回调并等待可能的协程结果（``parallel`` 使用）。"""
    result = _apply(callback, this_arg, args)
    if inspect.isawaitable(result):
        return await result
    return result
