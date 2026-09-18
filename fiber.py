"""fiber: plugin runtime and effect scheduler (port of ``core/src/fiber.ts``).

The official scheduler is promise based.  Python's asyncio is used instead:
when a loop is running, coroutines become tasks; otherwise they are driven to
completion synchronously so callers can keep using the plain API.

中文说明
--------
``Fiber`` 是「插件实例的生命周期载体」，也是 cordis 的调度核心。

一个 fiber 的职责：

1. **承载一个插件的一次加载**：插件可以是类、函数或带 ``apply`` 的对象；
   同一插件加载多次会产生多个 fiber，但共享同一 :class:`Runtime`。
2. **管理副作用（effect）**：插件里创建的监听器、定时器、服务都登记为 effect，
   随 fiber 卸载而自动清理，避免资源泄漏。
3. **响应依赖变化**：通过「epoch（纪元字符串）」比对，依赖就绪时加载（reload），
   依赖失效时卸载（unload），并在两者间自动流转。
4. **错误隔离**：插件抛异常时记录到 ``_error`` 并保持非激活，等待 ``update()``
   重置后重试。

状态机（:class:`FiberState`）::

    PENDING ──► LOADING ──► ACTIVE
                   ▲           │
                   │           ▼
                UNLOADING ◄────┘
                   │
                   ▼
                DISPOSED （uid 置空后）

关于「同步/异步」：官方调度基于 Promise，本移植改用 asyncio；当事件循环已在运行
时协程转成 task，否则用一个临时事件循环把它「跑到底」，从而让调用方可以继续使用
同步风格的 API（见文件末尾的 :func:`_spawn` / :func:`_run_and_drain`）。
"""

from __future__ import annotations

import asyncio
import enum
import inspect
from collections.abc import AsyncIterable, Awaitable, Callable, Generator, Iterable
from typing import TYPE_CHECKING, Any, ClassVar, TypeGuard, cast

from .registry import (
    Runtime,
)
from .utils import (
    DisposableList,
    ProtoDict,
    StackInfo,
    compose_error,
    get_symbol,
    get_traceable,
    is_constructor,
    is_nullable,
    is_object,
    set_symbol,
    symbols,
)

if TYPE_CHECKING:
    from .context import Context
    from .reflect import Impl
    from .registry import StandardSchemaIssue

# Port of TS ``Disposable<T = any>``. Bare ``Disposable[Any]`` is the default;
# use ``Disposable[T]`` where the payload type is known.
#: 「销毁函数」：调用即释放某个副作用；返回值 T 表示清理结果（通常是 None）
type Disposable[T] = Callable[[], T]

# Port of TS ``SyncEffect<T> = Disposable<T> | Iterable<Disposable<T>>``.
#: 同步副作用：单个销毁函数，或可迭代的一组销毁函数
type SyncEffect[T] = Disposable[T] | Iterable[Disposable[T]]

# Port of TS ``AsyncEffect<T> = Promise<Disposable<T>> | AsyncIterable<...>``.
# ``Awaitable`` models ``Promise``.
#: 异步副作用：可等待得到销毁函数，或异步产出多个销毁函数
type AsyncEffect[T] = Awaitable[Disposable[T]] | AsyncIterable[Disposable[T] | None]

# Port of TS ``Effect<T> = SyncEffect<T> | AsyncEffect<T>``.
#: 副作用的联合类型（``ctx.effect(...)`` 允许返回上述任意形态）
type Effect[T] = SyncEffect[T] | AsyncEffect[T]


class CordisError(Exception):
    """Port of TS ``CordisError``.

    cordis 自定义异常：以错误码（``code``）区分类型，默认取 ``codes`` 表中的
    描述作为消息。目前只有 ``INACTIVE_EFFECT`` 一种：在已卸载的上下文上创建
    effect。
    """

    INACTIVE_EFFECT = "cannot create effect on inactive context"

    codes: ClassVar[dict[str, str]] = {"INACTIVE_EFFECT": INACTIVE_EFFECT}

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or self.codes.get(code, code))


class ValidationError(TypeError):
    """Port of TS ``ValidationError``.

    配置校验失败时抛出，``issues`` 保存全部问题项；消息会被渲染成
    ``invalid config:`` 加逐行 ``  - 描述 (at 字段路径)`` 的形式，便于定位。
    """

    name = "ValidationError"

    def __init__(
        self,
        issues: StandardSchemaIssue
        | dict[str, Any]
        | object
        | Iterable[StandardSchemaIssue | dict[str, Any] | object],
    ) -> None:
        self.issues = issues
        lines = ["invalid config:"]
        for issue in issues if isinstance(issues, (list, tuple)) else [issues]:
            # 兼容三种问题项形态：对象属性、字典键、纯字符串
            message = getattr(issue, "message", None) or (
                issue.get("message") if isinstance(issue, dict) else str(issue)
            )
            path = getattr(issue, "path", None) or (
                issue.get("path") if isinstance(issue, dict) else None
            )
            if path:
                lines.append(f"  - {message} (at {'.'.join(str(p) for p in path)})")
            else:
                lines.append(f"  - {message}")
        super().__init__("\n".join(lines))


class EffectRunner[T]:
    """Port of TS ``interface EffectRunner``.

    Drives ``Fiber._execute``.  ``epoch`` is ``bool`` for effects (armed latch)
    and ``str`` for fibers (dependency epoch string).

    副作用驱动器：``_execute`` 通过它拿到「要执行什么」与「把销毁函数交给谁」。

    字段:
        execute: 返回副作用的函数（插件提供的逻辑）。
        epoch: fiber 用字符串表示「依赖纪元」；effect 用布尔表示「是否已解除」
            （``True`` 表示尚未销毁，可防重复销毁）。
        collect: 收集销毁函数的回调。
        get_outer_stack: 长栈信息提供者（出错时附加调用方栈）。
    """

    __slots__ = ("collect", "epoch", "execute", "get_outer_stack")

    def __init__(
        self,
        execute: Callable[[], Any],
        epoch: T,
        collect: Callable[[Disposable[Any]], None],
        get_outer_stack: Callable[[], list[str]] | None = None,
    ) -> None:
        self.execute = execute
        self.epoch = epoch
        self.collect = collect
        self.get_outer_stack = get_outer_stack


class FiberState(enum.IntEnum):
    """fiber 状态机（对应官方 ``FiberState``）。

    * PENDING：已创建但尚未开始绑定插件；
    * LOADING：正在加载（执行插件本体）；
    * ACTIVE：正常运行；
    * FAILED：加载或运行中出错（等待 ``update()`` 恢复）；
    * DISPOSED：已销毁（``uid`` 为 ``None``）；
    * UNLOADING：正在卸载（执行清理逻辑）。
    """

    PENDING = 0
    LOADING = 1
    ACTIVE = 2
    FAILED = 3
    DISPOSED = 4
    UNLOADING = 5


class EffectMeta:
    """Port of TS ``interface EffectMeta``.

    副作用元信息：``label`` 是人类可读标签（调试用），``children`` 记录嵌套的
    子 effect 元信息，从而在 ``fiber.get_effects()`` 中形成树状结构。
    """

    __slots__ = ("children", "label")

    def __init__(self, label: str = "anonymous") -> None:
        self.label = label
        self.children: list[EffectMeta] = []

    def __repr__(self) -> str:
        return f"EffectMeta({self.label!r}, children={len(self.children)})"


#: 「非激活」纪元标记：fiber 的依赖未就绪（或已卸载）时使用
INACTIVE = "__INACTIVE__"


def resolve_config(runtime: Runtime, config: Any) -> Any:
    """Port of TS ``resolveConfig``: validate through the standard schema.

    用插件声明的 ``Config``（标准 Schema）校验并转换配置：

    * 未声明 Schema → 原样返回；
    * 校验通过 → 返回转换后的值（支持返回对象或 ``{"value": ...}`` 字典两种形式）；
    * 校验失败 → 抛 :class:`ValidationError`；
    * 异步校验 → 直接报错（本移植不支持异步 Schema）。
    """
    schema = runtime.Config
    if schema is None:
        return config
    # 兼容三种声明形式：``standard`` 属性、``~standard`` 属性、``{"~standard": ...}`` 字典
    standard = getattr(schema, "standard", None) or getattr(schema, "~standard", None)
    if standard is None and isinstance(schema, dict):
        standard = schema.get("~standard")
    validator = getattr(standard, "validate", None) if standard is not None else None
    if validator is None:
        validator = getattr(schema, "validate", None)
    if validator is None:
        raise TypeError("invalid config schema")
    result = validator(config)
    if inspect.isawaitable(result):
        raise TypeError("async config validation is not supported")
    if isinstance(result, dict):
        issues = result.get("issues")
        if issues:
            raise ValidationError(issues)
        return result.get("value")
    issues = getattr(result, "issues", None)
    if issues:
        raise ValidationError(issues)
    return getattr(result, "value", result)


class Fiber:
    """Port of the official ``Fiber``.

    插件实例的生命周期载体。构造时会立即在父 fiber 上创建一个 effect 来完成
    「绑定插件」的工作；随后依赖是否就绪决定它进入 ACTIVE 还是保持非激活。
    """

    uid: int | None
    parent: Context
    ctx: Context
    context: Context
    config: Any
    runtime: Runtime | None
    state: FiberState
    store: dict[str, Impl] | None
    inertia: asyncio.Task[None] | _Completed | None
    inject: dict[str, Any | None]
    _hooks: dict[str, DisposableList[Callable[..., Any]]]
    _disposables: DisposableList[Disposable[Any]]
    _store: dict[str, Impl]
    _error: BaseException | None
    _runner: EffectRunner[str]

    def __init__(
        self,
        parent: Context,
        config: Any,
        inject: dict[str, Any | None] | None = None,
        runtime: Runtime | None = None,
        get_outer_stack: Callable[[], list[str]] | None = None,
    ) -> None:
        """创建 fiber。

        参数:
            parent: 父上下文（fiber 的 ``ctx`` 由它 ``extend`` 得到）。
            config: 插件配置（会在绑定时用 Schema 校验）。
            inject: 依赖声明（``{服务名: 配置}``）。
            runtime: 插件运行时记录；``None`` 表示这是「根 fiber」。
            get_outer_stack: 长栈提供者，默认自动抓取当前栈。
        """
        from .utils import Tracker, build_outer_stack

        self.parent = parent
        self.config = config
        self.inject = inject if inject is not None else {}
        self.runtime = runtime
        self.uid = None
        self.state = FiberState.PENDING
        self.store = None
        self.inertia = None
        self._hooks = {}
        self._disposables = DisposableList[Disposable[Any]]()
        self._store: dict[str, Impl] = {}
        self._error: BaseException | None = None
        self._runner: EffectRunner[str]
        self._get_outer_stack = get_outer_stack or build_outer_stack()
        # no_shadow：fiber 自身不做定义点追踪
        set_symbol(self, symbols.tracker, Tracker(property="fiber", no_shadow=True))

        def collect(dispose: Disposable[Any]) -> None:
            """收集 effect 产生的销毁函数，卸载时统一执行。"""
            self._disposables.push(dispose)

        if runtime is None:
            # root fiber —— 根 fiber：不承载插件，恒为 ACTIVE
            self.uid = 0
            self.ctx = self.context = parent
            self.store = {}
            self.state = FiberState.ACTIVE
            self._runner = EffectRunner(lambda: None, "", collect, self._get_outer_stack)
            self.dispose = self._root_dispose  # type: ignore[method-assign]
            return

        self.uid = parent.registry.counter
        self.ctx = self.context = parent.extend({"fiber": self})

        # 把依赖配置写进 intercept 层：依赖方（服务）读取时可看到针对自己的配置
        # 只有非 None 的依赖才需要配置层
        inject_entries = {k: v for k, v in self.inject.items() if not is_nullable(v)}
        if inject_entries:
            current = self.ctx.__get_symbol__(symbols.intercept, ProtoDict())
            if isinstance(current, ProtoDict):
                intercept = ProtoDict(proto=current)
            else:
                intercept = ProtoDict(mapping=current if isinstance(current, dict) else None)
            for name, value in inject_entries.items():
                intercept[name] = value
            self.ctx.__set_symbol__(symbols.intercept, intercept)

        self._runner = EffectRunner(self._run_plugin, INACTIVE, collect, self._get_outer_stack)
        # 广播「新插件出现」，供需要感知插件列表的扩展使用
        self.context.events.emit("internal/plugin", self)

        # 逐个检查依赖是否已就绪，填充 _store
        for name in list(self.inject.keys()):
            self._check_impl(name)

        # 在父 fiber 上创建 effect：effect 存活期间插件保持加载，销毁即卸载
        self.dispose = parent.fiber.effect(lambda: self._bind_plugin(), "ctx.plugin()")  # type: ignore[method-assign]

    # -- lifecycle —— 生命周期 -----------------------------------------------
    def _bind_plugin(self) -> Callable[[], Awaitable[None]]:
        """The effect body that registers/unregisters this plugin fiber.

        注册插件并返回注销逻辑：

        1. 把自身加入 ``runtime.fibers``；
        2. 校验配置（失败则记日志、置 ``_error``，不抛出以免中断其他插件）；
        3. 计算依赖纪元并触发加载；
        4. 返回的 ``dispose`` 负责反向操作：置空 ``uid``、广播事件、从
           runtime 删除（末个 fiber 时连 runtime 一起删）、进入非激活并等待
           正在进行的加载/卸载结束。
        """
        runtime = self.runtime
        if runtime is None:
            raise RuntimeError("cannot bind a root fiber as a plugin")
        remove = runtime.fibers.push(self)

        try:
            self.config = resolve_config(runtime, self.config)
            self._refresh()
        except BaseException as error:  # noqa: BLE001
            # 配置非法：记录错误并保持非激活，等待 update() 修正
            self.ctx.logger.error(error)
            self._error = error

        async def dispose() -> None:
            self.uid = None
            self.context.events.emit("internal/plugin", self)
            if self.ctx.registry.has(runtime.callback):
                remove()
                if not runtime.fibers.length:
                    # 该插件的最后一个 fiber 已移除，连同 runtime 一起清理
                    self.ctx.registry.delete(runtime.callback)
            self._set_epoch(INACTIVE)
            while self.inertia is not None:
                await self.inertia

        return dispose

    @property
    def name(self) -> str:
        """fiber 名称：优先取插件名，否则沿父链上溯，最终回退 ``"root"``。"""
        fiber: Fiber = self
        while True:
            if fiber.runtime is not None and fiber.runtime.name:
                return fiber.runtime.name
            parent_fiber = fiber.parent.fiber if fiber.parent is not None else None
            if parent_fiber is None or parent_fiber is fiber:
                break
            fiber = parent_fiber
        return "root"

    def assert_active(self) -> None:
        """断言 fiber 处于激活状态；否则抛 :class:`CordisError`。

        新建 effect / 注册监听器 / 加载插件前都会调用，防止在已卸载的上下文
        上继续产生副作用。
        """
        if self.uid is not None:
            return
        raise CordisError("INACTIVE_EFFECT")

    # -- effect engine —— 副作用引擎 -----------------------------------------
    def _execute(self, runner: EffectRunner[Any]) -> Any:
        """Port of ``_execute``: dispatch on the returned effect shape.

        执行 ``runner.execute()`` 并按返回值的形态分派处理：

        ==================================  ==================================
        返回值形态                            处理方式
        ==================================  ==================================
        可调用（销毁函数）                     直接收集
        ``None``                             无事发生
        可等待对象（协程）                     包成协程，await 后收集其返回值
        可迭代对象（同步批量）                 逐个收集
        异步可迭代对象                        逐项 await 收集，纪元变化即中止
        其他                                  ``TypeError("Invalid effect")``
        ==================================  ==================================

        整个流程包在 :func:`compose_error` 中，出错时自动附加外层栈信息。
        """
        old_epoch = runner.epoch

        def safe_collect(dispose: Disposable[Any] | None) -> None:
            """校验并收集销毁函数；``None`` 表示该 effect 无需清理。"""
            if dispose is None:
                return
            if callable(dispose):
                runner.collect(dispose)
                return
            raise TypeError("Invalid effect")

        def body(info: StackInfo) -> Any:
            effect = runner.execute()
            if callable(effect):
                return runner.collect(effect)
            if is_nullable(effect):
                return None
            if not is_object(effect):
                raise TypeError("Invalid effect")
            if hasattr(effect, "__await__"):
                # 协程：await 之后其返回值才是销毁函数
                async def _await() -> None:
                    value = await effect
                    safe_collect(value)
                return _await()
            if isinstance(effect, Iterable) and not isinstance(effect, (str, bytes, dict)):
                # 同步批量：逐个收集销毁函数（生成器也走这条路径）
                info.error = RuntimeError()
                iterator = iter(effect)
                while True:
                    try:
                        result = next(iterator)
                    except StopIteration:
                        return None
                    safe_collect(result)
            if hasattr(effect, "__aiter__"):
                # 异步批量：每产出一项就校验纪元，fiber 已切换状态则立刻停止
                async_effect = cast(AsyncIterable[Disposable[Any] | None], effect)
                async def _consume() -> None:
                    await asyncio.sleep(0)  # force async stack trace
                    info.error = RuntimeError()
                    async for result in async_effect:
                        if runner.epoch != old_epoch:
                            # 纪元变化说明 fiber 已开始卸载，停止继续消费
                            return
                        safe_collect(result)
                return _consume()
            raise TypeError("Invalid effect")

        return compose_error(body, runner.get_outer_stack)

    def effect(
        self, execute: Callable[[], Effect[Any] | None], label: str = "anonymous"
    ) -> Disposable[Any]:
        """Port of ``effect``: create an effect that lives as long as its fiber.

        创建副作用：``execute`` 立即执行，其返回的销毁函数被登记到当前 fiber；
        fiber 卸载时按「后注册先销毁」的顺序统一执行。

        返回的 ``wrapper`` 本身即销毁函数，并附带若干属性：

        * ``wrapper.meta``：:class:`EffectMeta`，描述该 effect（含子 effect 树）；
        * ``wrapper.dispose_async``：仅清理不等待的版本；
        * ``wrapper.then(...)``：等待 effect 完成后清理（``await`` 风格）。

        幂等性：``runner.epoch`` 是「已解除」闩锁，重复调用 ``wrapper()`` 只会
        执行一次清理。异步 effect 期间的异常会先把已产生的销毁函数跑完再抛出，
        避免资源泄漏。
        """
        self.assert_active()

        disposables: list[Disposable[Any]] = []
        wrapper_ref: list[Disposable[Any]] = []

        def dispose() -> Awaitable[Any] | None:
            """执行所有销毁函数（逆序），返回需要 await 的尾链（若有）。"""
            task: Awaitable[Any] | None = None
            for disposable in reversed(disposables[:]):
                if task is not None:
                    # 前面已有异步销毁在排队，串行接上去，保证顺序
                    task = _chain(task, disposable)
                else:
                    result = disposable()
                    if _is_thenable(result):
                        task = result
            disposables.clear()
            if wrapper_ref:
                self._disposables.delete(wrapper_ref[0])
            return task

        meta = EffectMeta(label)
        runner: EffectRunner[bool] = EffectRunner(
            execute,
            True,
            lambda dispose_value: _effect_collect(dispose_value, disposables, self, meta),
            self._get_outer_stack,
        )

        task: Awaitable[Any] | None = None
        try:
            task = self._execute(runner)
        except BaseException:
            # 执行失败：先把已收集的清理项跑掉，再向上抛
            dispose()
            raise

        if _is_thenable(task):
            # 异步 effect：包一层守卫，未捕获的异常先清理再记日志
            task = _guard_task(task, dispose, self.ctx)

        def wrapper() -> Any:
            """销毁函数：闩锁保证只执行一次清理。"""
            if not runner.epoch:
                return None
            runner.epoch = False
            if _is_thenable(task):
                # 异步 effect 需先等它完成（或失败），再执行清理
                return _chain(task, dispose)
            return dispose()

        set_symbol(wrapper, symbols.effect, meta)
        wrapper_ref.append(wrapper)

        def dispose_async() -> Any:
            """只清理不等待：立即解除闩锁并执行清理，不等待 pending 任务。"""
            if not runner.epoch:
                return None
            runner.epoch = False
            return dispose()

        wrapper.dispose_async = dispose_async  # type: ignore[attr-defined]
        wrapper.meta = meta  # type: ignore[attr-defined]
        wrapper.then = lambda on_fulfilled=None, on_rejected=None: _effect_then(  # type: ignore[attr-defined]
            task, dispose_async, on_fulfilled, on_rejected
        )
        # 把 wrapper 自身也登记进 fiber：fiber 卸载时它会触发整条清理链
        disposables.append(self._disposables.push(wrapper))
        return wrapper

    def get_effects(self) -> list[EffectMeta]:
        """Port of ``getEffects``.

        返回当前 fiber 上所有 effect 的元信息（用于调试面板展示 effect 树）。
        """
        result = []
        for disposable in list(self._disposables):
            meta = get_symbol(disposable, symbols.effect)
            if meta is not None:
                result.append(meta)
        return result

    def get_state(self) -> FiberState:
        """Port of ``_getState``.

        实时状态（不依赖缓存字段）：``uid`` 为空即 DISPOSED；有错误即 FAILED；
        否则把状态机的值转换为对外的三态（PENDING 会被视为 LOADING）。
        """
        if self.uid is None:
            return FiberState.DISPOSED
        if self._error is not None:
            return FiberState.FAILED
        if self._runner.epoch != INACTIVE:
            return FiberState.ACTIVE
        return FiberState.PENDING

    def _update_state(self, callback: Callable[[], FiberState | None]) -> None:
        """Port of ``_updateState``: state plus conditional service notification.

        更新状态并（在需要时）通知依赖方：

        1. 回调可返回新状态；返回 ``None`` 表示「按当前情况实时推断」；
        2. 状态有变化则广播 ``internal/status``；
        3. 仅当在「ACTIVE ↔ 非 ACTIVE」之间翻转时，才逐个通知本 fiber 提供的服务
           —— 只针对每个服务的一次翻转，避免重复触发依赖方的重载。

        ``callback`` 形式的设计是为了让状态判断与异步流程解耦：异步流程结束时才
        根据最新纪元决定落到哪个状态。
        """
        old_state = self.state
        result = callback()
        self.state = result if result is not None else self.get_state()
        if old_state == self.state:
            return
        self.context.events.emit("internal/status", self, old_state)
        # only notify changes between ACTIVE and NON-ACTIVE states
        if old_state is not FiberState.ACTIVE and self.state is not FiberState.ACTIVE:
            return
        # official code walks the global store and pings every service provided
        # by this fiber, so dependencies can (re)bind exactly once per flip.
        for key, impl in list(self.ctx.reflect.store.items()):
            if impl.fiber is not self:
                continue
            self.ctx.reflect.notify([impl.name])

    def _check_impl(self, name: str) -> None:
        """Port of ``_checkImpl``.

        检查某个依赖在当前上下文中是否可用：

        * 找不到实现（或提供者未激活）→ 从 ``_store`` 移除；
        * 实现带 ``check`` 函数 → 调用它（兼容「无参」与「接收服务值」两种签名），
          返回假值或抛异常都视为不可用（异常会被记日志）；
        * 通过则写入 ``_store``，供 :meth:`_refresh` 计算纪元。
        """
        impl = self.ctx.reflect._get_impl(name, True)
        if impl is None:
            self._store.pop(name, None)
            return
        if impl.check is not None:
            try:
                check = impl.check
                if callable(check):
                    result = _invoke_check(check, get_traceable(self.ctx, impl.value))
                else:
                    result = check
                if not result:
                    self._store.pop(name, None)
                    return
            except BaseException as error:  # noqa: BLE001
                impl.fiber.ctx.logger.error(error)
                self._store.pop(name, None)
                return
        self._store[name] = impl

    def _refresh(self) -> None:
        """Port of ``_refresh``: recompute the dependency epoch.

        重算依赖纪元：把每个依赖的「提供者 fiber uid」拼成字符串，任何依赖缺失
        或提供者已销毁则纪元为 :data:`INACTIVE`。纪元变化是触发加载/卸载的唯一依据。
        """
        if self.runtime is None:
            return
        epoch = ""
        for name in self.inject:
            impl = self._store.get(name)
            if impl is None or impl.fiber.uid is None:
                epoch = INACTIVE
                break
            epoch += f":{impl.fiber.uid}"
        self._set_epoch(epoch)

    def _set_epoch(self, epoch: str) -> None:
        """Port of ``_setEpoch``.

        设置依赖纪元并按需触发加载/卸载：

        * 纪元未变化 → 直接返回；
        * 处于 FAILED（``_error`` 非空）→ 只在 ``update()`` 清空错误后才恢复；
        * 若当前有未完成的加载/卸载（``inertia``）→ 交给它结束时接力；
        * ``INACTIVE → 有效纪元``：先发布 ``LOADING`` 状态再启动加载（异步体可能
          在无事件循环时同步完成，状态必须先对外可见）；
        * 其他变化（含 ``有效纪元 → INACTIVE``）：进入卸载流程。
        """
        old_epoch = self._runner.epoch
        if epoch == old_epoch:
            return
        # a failed fiber only recovers through update(), which clears _error
        if self._error is not None:
            return
        self._runner.epoch = epoch
        if isinstance(self.inertia, _Completed):
            # 已结算的临时任务不再需要等待，清空以便发起新的生命周期流程
            self.inertia = None
        if self.inertia is not None:
            return
        if epoch != INACTIVE and old_epoch == INACTIVE:
            # The state must be published before the load starts: the async body
            # may complete synchronously when no loop is running.
            self._update_state(lambda: FiberState.LOADING)
            self._start_inertia(self._reload())
        else:
            self._update_state(lambda: FiberState.UNLOADING)
            self._start_inertia(self._unload())

    def _start_inertia(self, awaitable: Awaitable[None]) -> None:
        """Start lifecycle work without retaining an already-settled task.

        启动一次生命周期工作（加载或卸载）：若协程已同步完成（``_Completed``），
        则不保留 ``inertia``，避免后续 ``wait()`` 空等。
        """
        task = _spawn(awaitable)
        if not isinstance(task, _Completed):
            self.inertia = task

    async def _reload(self) -> None:
        """Port of ``_reload``.

        加载插件：先给依赖方一份 ``_store`` 快照（``store``），再执行插件本体；
        失败则记录错误并把纪元置为 :data:`INACTIVE`（保持非激活，等待修复）。
        结束时根据纪元是否变化决定「稳定下来」还是立即转入卸载。
        """
        self.store = dict(self._store)
        old_epoch = self._runner.epoch
        try:
            await asyncio.sleep(0)
            await _maybe_await(self._execute(self._runner))
        except BaseException as reason:  # noqa: BLE001
            self.ctx.logger.error(reason)
            self._error = reason
            self._runner.epoch = INACTIVE

        def finish() -> FiberState | None:
            if self._runner.epoch == old_epoch:
                # 加载期间依赖没变化，进入稳定状态
                self.inertia = None
                return None
            self._start_inertia(self._unload())
            return FiberState.UNLOADING

        self._update_state(finish)

    async def _unload(self) -> None:
        """Port of ``_unload``.

        卸载插件：清空 effect（逆序清理）、等待全部清理完成、丢弃 ``store`` 快照。
        结束时同样根据纪元决定「稳定」还是「立即重新加载」。
        """
        disposables = self._disposables.clear()
        await asyncio.gather(
            *(_unload_one(dispose, self) for dispose in disposables),
            return_exceptions=True,
        )
        self.store = None

        def finish() -> FiberState | None:
            if self._runner.epoch == INACTIVE:
                self.inertia = None
                return None
            self._start_inertia(self._reload())
            return FiberState.LOADING

        self._update_state(finish)

    async def wait(self) -> Fiber:
        """Port of ``await()``.

        等待当前生命周期工作完成；若加载过程中出错则抛出该错误。
        循环处理「等待期间又被触发了新工作」的情况，确保返回时真正稳定。
        """
        while self.inertia is not None:
            pending = self.inertia
            try:
                await pending
            finally:
                if self.inertia is pending:
                    self.inertia = None
        if self._error is not None:
            raise self._error
        return self

    # ``await fiber`` support —— 支持 ``await fiber`` 语法
    def __await__(self) -> Generator[Any, None, Fiber]:
        return self.wait().__await__()

    async def restart(self) -> None:
        """Port of ``restart``：强制卸载后重新加载当前插件。

        实现方式是把纪元直接置为 :data:`INACTIVE`（触发卸载），再 ``_refresh()``
        重算纪元（触发加载），最后等待稳定。
        """
        fiber = self.ctx.fiber
        fiber.assert_active()
        fiber._set_epoch(INACTIVE)
        fiber._refresh()
        await fiber.wait()

    def update(self, config: Any, no_save: bool = False) -> Any:
        """Port of ``update``.

        更新插件配置并重新加载。走 ``internal/update`` 瀑布事件，使 fiber 私有
        钩子可以拦截/改写更新过程（``default`` 是默认实现：写入配置、清空错误、
        重启）。

        参数:
            config: 新配置（有 Schema 时先校验）。
            no_save: 是否跳过持久化（框架层语义，本移植只透传）。

        返回 ``None``（更新被钩子短路）或可等待对象（异步更新任务）。
        """
        fiber = self.ctx.fiber
        fiber.assert_active()
        if fiber.runtime is not None:
            config = resolve_config(fiber.runtime, config)

        def default() -> Any:
            fiber.config = config
            # 清空错误，让 _set_epoch 允许重新加载（FAILED 状态只能由此恢复）
            fiber._error = None
            return fiber.restart()

        result = fiber.context.waterfall(fiber, "internal/update", config, no_save, default)
        if result is None:
            return None
        if _is_thenable(result):
            # 异步更新：附带错误日志后返回任务
            response = result

            async def _observe() -> Any:
                try:
                    return await response
                except BaseException as error:
                    fiber.ctx.logger.error(error)
                    raise

            return _spawn(_observe())
        return result

    # -- plugin callback —— 插件回调执行 -------------------------------------
    def _run_plugin(self) -> Any:
        """Port of ``runner.execute`` inside the constructor.

        执行插件本体，两种形态：

        * **类插件**：以 ``callback(ctx, config)`` 实例化，随后依次执行
          ``symbols.initHooks``（``@Inject`` 装饰器注册的钩子，以及从类方法上收集
          的钩子），最后调用实例的 ``init`` 方法；
        * **函数插件**：直接 ``callback(ctx, config)``。

        实例化的返回值（通常是被创建的实例或 None）会被视为 effect 本体，
        因此插件可以直接返回销毁函数。
        """
        if self.runtime is None:
            return None
        callback = self.runtime.callback
        if is_constructor(callback):
            instance = callback(self.ctx, self.config)
            # 实例自带的 initHooks（例如构造过程中收集的）
            hooks = get_symbol(instance, symbols.initHooks)
            if hooks is None:
                hooks = getattr(instance, "init_hooks", None)
            if hooks:
                for hook in hooks:
                    hook()

            # Python method decorators cannot install a TC39-style instance
            # initializer, so collect their hooks from the class hierarchy at
            # the same point where the official constructor runs them.
            # 从类方法上收集 @Inject 装饰器注册的钩子；用 id 去重避免继承重复执行
            seen_hooks: set[int] = set()
            for cls in type(instance).__mro__:
                for member in vars(cls).values():
                    descriptor = member
                    if isinstance(member, (staticmethod, classmethod)):
                        member = member.__func__
                    method_hooks = get_symbol(member, symbols.initHooks)
                    if not method_hooks:
                        continue
                    for hook in method_hooks:
                        marker = id(hook)
                        if marker in seen_hooks:
                            continue
                        seen_hooks.add(marker)
                        hook_metadata = get_symbol(hook, symbols.metadata, {})
                        if isinstance(hook_metadata, dict) and hook_metadata.get("inject_hook"):
                            hook(instance, descriptor)
                        else:
                            hook(instance)
            # 最后调用 init（服务类常用：构造完再做初始化）
            init = get_symbol(instance, symbols.init)
            if init is None:
                init = getattr(instance, "init", None)
            return init() if callable(init) else None
        return callback(self.ctx, self.config)

    # -- root fiber —— 根 fiber ----------------------------------------------
    async def _root_dispose(self) -> None:
        """根 fiber 的销毁实现：不真正销毁，而是重启（清空后重建根级 effect）。"""
        await self.restart()

    def __repr__(self) -> str:
        return f"Fiber <{self.name}> uid={self.uid} state={self.state.name}"


# ---------------------------------------------------------------------------
# helpers —— 辅助函数
# ---------------------------------------------------------------------------


def _effect_collect(
    dispose_value: Disposable[Any],
    disposables: list[Disposable[Any]],
    fiber: Fiber,
    meta: EffectMeta,
) -> None:
    """Port of the ``collect`` closure inside ``effect``.

    收集一个销毁函数：加入本地列表（卸载时执行），并从 fiber 全局列表中移除
    （它已由本 effect 统一管理）；若该销毁函数本身是个子 effect，则挂到
    ``meta.children`` 上形成 effect 树。
    """
    disposables.append(dispose_value)
    fiber._disposables.delete(dispose_value)
    child = get_symbol(dispose_value, symbols.effect)
    if child is not None:
        meta.children.append(child)


def _effect_then(
    task: Awaitable[Any] | None,
    dispose_async: Callable[[], Any],
    on_fulfilled: Callable[[Any], Any] | None,
    on_rejected: Callable[[BaseException], Any] | None,
) -> Awaitable[Any]:
    """``wrapper.then``: wait for the task, then dispose.

    模拟 ``promise.then``：先等待 effect 任务结束（无论成功失败），然后执行清理；
    可选地把结果/异常转交给 ``on_fulfilled`` / ``on_rejected``。
    """

    async def _run() -> Any:
        try:
            if _is_thenable(task):
                await task
        finally:
            dispose_async()
        return None

    result = _spawn(_run())
    if (on_fulfilled is not None or on_rejected is not None) and _is_thenable(result):
        return _chain(result, None) if on_fulfilled is None else _run_then(  # pragma: no cover
            result, on_fulfilled, on_rejected
        )
    return result


async def _run_then(
    task: Awaitable[Any],
    on_fulfilled: Callable[[Any], Any] | None,
    on_rejected: Callable[[BaseException], Any] | None,
) -> Any:
    """等待 ``task`` 并把结果/异常交给回调（``then`` 的两分支实现）。"""
    try:
        value = await task
    except BaseException as error:
        if on_rejected is not None:
            return on_rejected(error)
        raise
    return on_fulfilled(value) if on_fulfilled is not None else value


def _chain(
    task: Awaitable[Any] | None, disposable: Disposable[Any] | None
) -> Awaitable[None]:
    """``task.then(dispose)``.

    把「等待 ``task``」与「执行 ``disposable``」串成一条协程，用于保证异步清理的
    执行顺序。
    """

    async def _run() -> Any:
        if _is_thenable(task):
            await task
        if disposable is not None:
            result = disposable()
            if _is_thenable(result):
                await result
        return None

    return _spawn(_run())


def _guard_task(
    task: Awaitable[Any], dispose: Callable[[], Any], ctx: Context
) -> Awaitable[None]:
    """Prevent unhandled rejections: mirror ``task?.catch(dispose).catch(log)``.

    给异步 effect 加守卫：任务失败时先执行清理（以及清理本身返回的异步任务），
    再记录清理阶段的异常，最后「吞掉」错误返回 ``None``，避免未处理的异常
    影响调用方。
    """

    async def _run() -> Any:
        try:
            return await task
        except BaseException:  # noqa: BLE001
            result = dispose()
            if _is_thenable(result):
                try:
                    await result
                except BaseException as error:  # noqa: BLE001
                    ctx.logger.error(error)
            return None

    return _spawn(_run())


async def _unload_one(dispose: Disposable[Any], fiber: Fiber) -> None:
    """执行单个销毁函数：先让出一次事件循环，异常只记日志不向上抛。

    卸载阶段必须「尽力而为」：某个清理项失败不应阻断其它清理项。
    """
    try:
        await asyncio.sleep(0)
        result = dispose()
        if _is_thenable(result):
            await result
    except BaseException as reason:  # noqa: BLE001
        fiber.ctx.logger.error(reason)


async def _maybe_await(value: Any) -> Any:
    """反复 await 直到得到非可等待值（兼容「嵌套 thenable」的情况）。"""
    if _is_thenable(value):
        value = await value
    while _is_thenable(value):
        value = await value
    return value


def _is_thenable(value: Any) -> TypeGuard[Awaitable[Any]]:
    """是否为可等待对象（等价 JS 的 thenable 探测）。"""
    return is_object(value) and hasattr(value, "__await__")


def _invoke_check(check: Callable[..., Any], value: Any) -> Any:
    """调用服务的可用性检查函数，兼容两种签名。

    * ``check(value)``：接收服务实例（官方形式）；
    * ``check()``：无参形式（更符合 Python 习惯）。

    通过 ``inspect.signature`` 判断是否接受位置参数，无法反射时按传参调用。
    """
    try:
        parameters = inspect.signature(check).parameters.values()
    except (TypeError, ValueError):
        return check(value)
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    accepts_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    )
    if accepts_varargs or positional:
        return check(value)
    return check()


class _Completed:
    """Awaitable holding an already-settled result (mirrors a resolved promise).

    已结算结果的「伪任务」：在无事件循环时同步跑完的协程用它承载结果，
    从而 ``await`` 它仍能得到值或异常，保持与真实 task 一致的接口。
    """

    __slots__ = ("_error", "_value")

    def __init__(self, value: Any = None, error: BaseException | None = None) -> None:
        self._value = value
        self._error = error

    def __await__(self) -> Generator[Any, None, Any]:
        if self._error is not None:
            raise self._error
        return self._value
        yield  # pragma: no cover - makes this a generator function

    def __repr__(self) -> str:
        return f"_Completed(value={self._value!r}, error={self._error!r})"


def _spawn[T](awaitable: Awaitable[T]) -> asyncio.Task[T] | _Completed:
    """Schedule a coroutine, mirroring JS promise scheduling.

    * inside a running loop: a real task, so awaiting ``inertia`` behaves like
      awaiting a promise;
    * outside any loop: a private loop runs the coroutine *and* every task it
      cascades into, because reactor-style code (a service appearing mid-load)
      keeps chaining work.  The settled result is returned as an awaitable so
      callers such as ``await fiber.wait()`` keep working.

    调度入口，分两种情况：

    * **事件循环已在运行**：创建真实 task，``await`` 它的行为与 Promise 一致；
    * **没有事件循环**：临时新建循环把协程跑到底（见 :func:`_run_and_drain`），
      并以 :class:`_Completed` 返回结果，使同步调用方也能拿到 settle 后的状态。

    之所以需要「跑到底」：加载过程中可能继续派生新的任务（例如服务在加载中途出现），
    必须一并排干，否则会出现「协程未完成但调用方已继续执行」的竞态。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_and_drain(awaitable)
    return asyncio.ensure_future(awaitable)


def _run_and_drain(main: Awaitable[Any]) -> _Completed:
    """Run ``main`` on a fresh loop, then drain every task it spawned.

    在临时事件循环上运行 ``main``，并反复 gather「所有仍未完成的任务」直到全部
    结束（最多 10000 轮，防御性上限），最后把结果或异常包成 :class:`_Completed`。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        task = loop.create_task(_coerce(main))
        value: Any = None
        error: BaseException | None = None
        for _ in range(10000):
            pending = [item for item in asyncio.all_tasks(loop) if not item.done()]
            if not pending:
                break
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        if task.cancelled():
            error = asyncio.CancelledError()
        else:
            error = task.exception()
            if error is None:
                value = task.result()
        return _Completed(value, error)
    finally:
        # 收尾：关闭异步生成器、清理事件循环绑定，避免影响后续调用
        loop.run_until_complete(loop.shutdown_asyncgens())
        asyncio.set_event_loop(None)
        loop.close()


async def _coerce(awaitable: Awaitable[Any]) -> Any:
    """把可等待对象统一成协程（``create_task`` 需要协程而非任意 awaitable）。"""
    if inspect.isawaitable(awaitable):
        return await awaitable
    return awaitable
