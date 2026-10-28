"""ReflectService: service storage and the Context proxy handler.

Port of ``packages/core/src/reflect.ts``.  The official file installs a
``ProxyHandler`` on every ``Context``; here the same logic lives in
``ctx.reflect.handler_get`` / ``handler_set`` / ``handler_has``, which
``Context.__getattr__`` delegates to.

中文说明
--------
``ReflectService`` 是「服务仓库 + 属性访问路由器」，是 cordis 依赖注入的核心。

它主要做四件事：

1. **存储服务**：``store`` 以「服务键符号」为键保存 :class:`Impl`（服务实现记录，
   含提供者 fiber、当前值、可用性检查函数）。
2. **代理属性访问**：官方给每个 ``Context`` 装 ``ProxyHandler``；Python 无此机制，
   改由 ``Context.__getattr__`` 调用 :meth:`ReflectService.handler_get`。读取
   ``ctx.foo`` 时依次尝试：自有属性 → 类属性 → 访问器（accessor）→ 依赖注入查询。
3. **访问器与混入**：``accessor`` 声明计算属性，``mixin`` 把某服务的成员以别名挂到
   当前上下文上。
4. **依赖通知**：``provide`` / ``notify`` 在服务出现或消失时，重新检查并刷新
   依赖它的 fiber。

``handler_get`` 中「取不到属性」的路径值得留意：它不是简单报错，而是沿 fiber 树
向上回溯、比较隔离键（``interface``），并用 ``internal/get`` 瀑布事件给插件留出
补位机会；全部失败才抛出带有依赖提示的 ``AttributeError``。
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from .fiber import FiberState
from .utils import (
    ProtoDict,
    _Symbol,
    get_traceable,
    is_nullable,
    service_symbol,
    set_symbol,
    symbols,
    with_props,
)

if TYPE_CHECKING:
    from .context import Context
    from .fiber import Fiber

# 以 ``internal/`` 开头的事件名属于框架内部事件，派发时不触发 ``internal/dispatch``
_INTERNAL_PREFIX = "internal/"

#: 保留属性名：这两个名字在 JS 中具有特殊语义（构造器与 thenable 探测），
#: 必须绕过服务解析，否则会破坏对象协议。
RESERVED_WORDS = ("prototype", "then")
ValueT = TypeVar("ValueT")
#: 服务实现全局自增序号（进入依赖纪元，服务重登记即触发依赖方重载）
_IMPL_COUNTER = itertools.count(1)
#: 访问器 get 签名：``(使用点 ctx, receiver, error) -> 值``
AccessorGetter = Callable[["Context", Any, BaseException], Any]
#: 访问器 set 签名：``(使用点 ctx, 新值, receiver, error) -> 是否成功``
AccessorSetter = Callable[["Context", Any, Any, BaseException], bool]
AccessorOptions = Mapping[str, AccessorGetter | AccessorSetter | None]


def is_special_property(prop: Any) -> bool:
    """Port of ``isSpecialProperty``.

    Symbols, reserved words (``prototype``/``then``), numeric strings and names
    starting with ``_`` bypass the service resolution logic.

    这些名字不参与「服务解析」，直接按普通 Python 属性处理：

    * 非字符串（符号等）：符号槽位由 ``get_symbol`` 单独处理；
    * ``prototype`` / ``then``：JS 保留字，避免破坏对象协议；
    * ``_`` 开头：内部字段；
    * 纯数字：JS 数组索引风格，避免误判为服务名。
    """
    if not isinstance(prop, str):
        return True
    if prop in RESERVED_WORDS:
        return True
    if prop.startswith("_"):
        return True
    return prop.isdigit()


def _descriptor_value_static(descriptor: Any, instance: Any) -> Any:
    """Resolve a class-level descriptor against ``instance``.

    把类属性描述符解析为「实例视角」的值：``property``/静态方法/类方法都
    以 ``instance`` 为接收者调用 ``__get__``，其余原样返回。
    """
    if isinstance(descriptor, property):
        return descriptor.__get__(instance, type(instance))
    if isinstance(descriptor, (staticmethod, classmethod)):
        return descriptor.__get__(instance, type(instance))
    return descriptor


def enhance_error(error: BaseException) -> BaseException:
    """Port of ``enhanceError``: keep the message on the first stack line.

    Python 的 ``AttributeError`` 有时不带 ``args``（例如由 C 层抛出），
    补上 ``str(error)`` 有利于日志与长栈显示更清晰。
    """
    if not getattr(error, "__notes__", None) and not error.args:
        error.args = (str(error),)
    return error


@dataclass
class PropertyAccessor:
    """访问器对：``get`` 必需，``set`` 可选（缺省即只读属性）。"""

    get: AccessorGetter
    set: AccessorSetter | None = None


@dataclass
class Property:
    """``props`` 表里的一条声明：或是服务，或是访问器。"""

    type: Literal["service", "accessor"]
    accessor: PropertyAccessor | None = None

    @property
    def get(self) -> AccessorGetter | None:
        return self.accessor.get if self.accessor is not None else None

    @property
    def set(self) -> AccessorSetter | None:
        return self.accessor.set if self.accessor is not None else None


@dataclass(eq=False)
class Impl:
    """One entry in the service store (port of TS ``interface Impl``).

    一条服务实现记录：

    * ``uid``：全局自增序号（进入依赖纪元，服务重登记即触发依赖方重载）；
    * ``name``：服务名（字符串形式，便于日志与索引）；
    * ``ctx``：注册该服务时的上下文（``ctx.fiber`` 即提供者 fiber）；
    * ``isolate``：实际生效的隔离键（提供者分支被 isolate 搬迁后仍保留原键）；
    * ``dependents``：当前已绑定该实现的依赖方 fiber（销毁顺序控制用）；
    * ``value``：当前服务实例；
    * ``check``：可用性检查函数，返回假值时依赖方视为「依赖未就绪」；
    * ``release``：注销时等待依赖方清理完的释放回调（见 ``provide``）。
    """

    uid: int
    name: str
    ctx: Context
    isolate: _Symbol
    dependents: set[Fiber] = field(default_factory=set)
    value: Any = None
    check: Callable[[], bool] | None = None
    release: Callable[[], None] | None = None


def locate(ctx: Context, name: str) -> tuple[Fiber | None, Impl | None] | None:
    """Port of the ``locate`` helper (resolve a service access along fibers).

    从定义点 ``ctx`` 出发沿 fiber 树向上解析对服务 ``name`` 的一次访问，返回
    ``(fiber, impl)``（缺项为 ``None``）：

    * ``(None, impl)``：当前 fiber 自己在同一个隔离层提供了该实现；
    * ``(fiber, impl)``：实现来自 ``fiber`` 的已绑定视图（``committed``）；
    * ``(fiber, None)``：``fiber`` 声明了该依赖但尚未就绪（未激活）；
    * ``None``：没有任何 fiber 声明它，或查找途中命中了隔离边界（同名服务
      被 ``isolate`` 隔开，不允许穿透）。
    """
    key = ctx.interface(name)
    table = ctx.__get_symbol__(symbols.isolate, None)
    fiber = ctx.fiber
    while True:
        impl: Impl | None = None
        for provision in fiber.provisions:
            if provision.name == name and provision.ctx.interface(name) == key:
                impl = provision
        if impl is not None:
            return (None, impl)
        # 隔离边界检查：从访问点的隔离层走到当前 fiber 的隔离层，中途若发现
        # 某层自有该名字，说明同名服务被 isolate 隔开，查找到此为止
        fiber_table = fiber.ctx.__get_symbol__(symbols.isolate, None)
        while table is not fiber_table:
            if table is None or (isinstance(table, ProtoDict) and table.has_own(name)):
                return None
            table = getattr(table, "proto", None)
        impl = (fiber.committed or {}).get(name)
        if impl is not None or name in fiber.inject:
            return (fiber, impl)
        if fiber.runtime is None:
            return None
        parent = fiber.parent
        if parent is None:
            return None
        fiber = parent.fiber


class ReflectService:
    """Service registry facing a single :class:`Context`.

    面向单个上下文的「服务仓库视图」：子上下文共享同一份 ``store``/``props``，
    但拥有各自的 ``ctx``，从而区分「谁在注册」与「谁在访问」。
    """

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        #: 服务键符号 -> 实现记录（全上下文树共享同一份存储）
        self.store: dict[_Symbol, Impl] = {}
        #: 属性名 -> 声明（服务或访问器），决定某名字由谁提供
        self.props: dict[str, Property] = {}
        # `symbols.tracker` with `noShadow: true` (the official constructor does
        # this through `defineProperty`).
        # ``noShadow=True``：ReflectService 自身不需要定义点追踪
        set_symbol(self, symbols.tracker, _REFLECT_TRACKER)
        self._bootstrap()

    # -- constructor mixins —— 构造期的 mixin 调用 ---------------------------
    def _bootstrap(self) -> None:
        """Port of the four ``this.mixin(...)`` calls in the constructor.

        把四个核心服务的方法以同名别名挂到当前上下文上，因此可以直接写
        ``ctx.get(...)``、``ctx.on(...)``、``ctx.plugin(...)``，而无需
        ``ctx.reflect.get(...)``、``ctx.events.on(...)``、``ctx.registry.plugin(...)``。
        """
        self.mixin("reflect", ["get", "set", "provide", "accessor", "mixin"])
        self.mixin("fiber", ["runtime", "effect"])
        self.mixin("registry", ["inject", "plugin"])
        self.mixin("events", ["on", "once", "parallel", "emit", "serial", "bail", "waterfall"])

    def for_context(self, ctx: Context) -> ReflectService:
        """派生出面向 ``ctx`` 的同构服务（共享 store/props）。"""
        service = object.__new__(type(self))
        service.ctx = ctx
        service.store = self.store
        service.props = self.props
        set_symbol(service, symbols.tracker, _REFLECT_TRACKER)
        return service

    def __get_symbol__(self, symbol: Any, default: Any = None) -> Any:
        # 只特化 tracker；其余符号回退到 ``_cordis_<名字>`` 属性
        if symbol == symbols.tracker:
            return _REFLECT_TRACKER
        return getattr(self, "_cordis_" + symbol.name.replace(".", "_"), default)

    # -- proxy handler —— 代理处理器 -----------------------------------------
    def handler_get(self, ctx: Context, prop: str) -> Any:
        """Port of ``ReflectService.handler.get``.

        解析 ``ctx.<prop>``：特殊属性 / 自有属性 / 类属性直接返回；否则进入
        「服务解析」流程（访问器 → 瀑布事件 ``internal/get`` → 沿 fiber 树上溯）。
        """
        if is_special_property(prop):
            return self._own_get(ctx, prop)
        # `Reflect.has(target, prop)`: own storage or class-level members.  Plain
        # `hasattr` cannot be used here because it would re-enter `__getattr__`.
        # 注意：不能用 hasattr，否则会重新进入 Context.__getattr__ 造成递归
        if prop in ctx.__dict__ or hasattr(type(ctx), prop):
            value = self._own_get(ctx, prop)
            return get_traceable(ctx, value)

        error = AttributeError(f'cannot get property "{prop}" without inject')

        def _resolve() -> Any:
            definition = ctx.reflect.props.get(prop)
            if definition is not None and definition.type == "accessor":
                # 场景一：已声明的访问器（mixin / ctx.accessor 注册的计算属性）
                accessor = definition.accessor
                if accessor is not None:
                    return accessor.get(ctx, ctx.__get_symbol__(symbols.receiver), error)

            # a fiber-less def site has no `inject`, so it keeps the unchecked
            # root access (official comment).
            # 场景二：定义点（通常是插件类实例）所在 fiber 无 runtime：
            # 直接按非严格模式读取，等同于「根上下文可访问任意已注册服务」。
            def_site = ctx.__get_symbol__(symbols.shadow) or ctx
            if def_site.fiber.runtime is None:
                return ctx.reflect.get(prop, False)

            def _fallback() -> Any:
                """沿 fiber 树向上查找：命中已注入的实现，或给出精确报错。"""
                result = locate(def_site, prop)
                if result is None:
                    raise error
                declared, impl = result
                if impl is not None:
                    return get_traceable(ctx, impl.value)
                if declared is not None:
                    entry = declared.inject.get(prop)
                    if entry is not None and not entry.required:
                        # 可选依赖缺席：静默返回 None，不视为错误
                        return None
                    # 已在依赖声明中，但当前 fiber 未激活（尚未注入）
                    error.args = (
                        f'cannot get required service "{prop}" in inactive context',
                    )
                raise error

            # 场景三：先给插件一次补位机会（瀑布事件），否则执行 _fallback
            return ctx.events.waterfall("internal/get", ctx, prop, error, _fallback)

        try:
            return _resolve()
        except BaseException as exc:  # noqa: BLE001
            # 只有「属性缺失」这一种错误才做信息增强，其余原样抛出
            raise (enhance_error(exc) if exc is error else exc) from None

    def handler_set(self, ctx: Context, prop: str, value: Any) -> bool:
        """Port of ``ReflectService.handler.set``.

        写入 ``ctx.<prop>``：特殊属性/根上下文直接落属性；已声明访问器交给它的
        setter；已声明的服务则经 ``internal/set`` 瀑布事件写回 ``store``。
        """
        if is_special_property(prop):
            object.__setattr__(ctx, prop, value)
            return True

        error = AttributeError(f'cannot set property "{prop}" without provide')
        definition = ctx.reflect.props.get(prop)
        if definition is None:
            if ctx.fiber.runtime is None:
                # 根上下文允许任意挂属性（作为全局配置/测试替身的逃生口）
                object.__setattr__(ctx, prop, value)
                return True
            raise enhance_error(error)

        try:
            if definition.type == "accessor":
                accessor = definition.accessor
                if accessor is None or accessor.set is None:
                    return False
                return bool(accessor.set(ctx, value, ctx.__get_symbol__(symbols.receiver), error))
            return bool(
                ctx.events.waterfall(
                    "internal/set",
                    ctx,
                    prop,
                    value,
                    error,
                    lambda: ctx.reflect.set(prop, value, error),
                )
            )
        except BaseException as exc:  # noqa: BLE001
            raise (enhance_error(exc) if exc is error else exc) from None

    def handler_has(self, ctx: Context, prop: str) -> bool:
        """Port of ``ReflectService.handler.has``（对应 JS 的 ``in`` 运算符）。"""
        if is_special_property(prop):
            return prop in ctx.__dict__ or hasattr(type(ctx), prop)
        return prop in ctx.__dict__ or hasattr(type(ctx), prop) or prop in ctx.reflect.props

    @staticmethod
    def _own_get(ctx: Context, prop: str) -> Any:
        """Own storage first, then the class attribute (never ``__getattr__``).

        只查「实例 ``__dict__`` → 类 MRO」，绝不触发 ``__getattr__``：
        这是避免 ``Context`` 属性解析无限递归的关键。
        """
        state = ctx.__dict__
        if prop in state:
            return state[prop]
        for cls in type(ctx).__mro__:
            if prop in vars(cls):
                return _descriptor_value_static(vars(cls)[prop], ctx)
        raise AttributeError(prop)

    # -- storage —— 服务存储 -------------------------------------------------
    def _key(self, name: str) -> _Symbol | None:
        """取服务名对应的隔离键符号（由 ``isolate`` 层决定，用于区分同名服务）。"""
        return self.ctx.interface(name)

    def _get_impl(self, name: str, strict: bool = True) -> Impl | None:
        """取服务实现；``strict=True`` 时要求提供者 fiber 处于 ACTIVE 状态。"""
        key = self._key(name)
        if key is None:
            return None
        impl = self.store.get(key)
        if impl is None:
            return None
        if strict and impl.ctx.fiber.state is not FiberState.ACTIVE:
            return None
        return impl

    def has(self, name: str) -> bool:
        """服务是否已注册（不要求激活）。"""
        return self._get_impl(name, False) is not None

    def get(self, name: str, strict: bool = True, **kwargs: Any) -> Any:
        """按名字读服务值；不可用或不存在时返回 ``None``。

        返回前会经 :func:`get_traceable` 包装，使调用者能拿到正确的上下文信息。
        """
        impl = self._get_impl(name, strict)
        if impl is None:
            return None
        return get_traceable(self.ctx, impl.value)

    def set(self, name: str, value: Any, error: BaseException | None = None, **kwargs: Any) -> bool:
        """更新服务值。

        抛出:
            AttributeError: 服务未声明，或试图在「非提供者 fiber」中修改
                （官方语义：服务只能由提供它的 fiber 修改，避免跨插件覆盖）。
        """
        key = self._key(name)
        impl = self.store.get(key) if key is not None else None
        if impl is None:
            raise AttributeError(f'cannot set property "{name}" without provide')
        if impl.ctx.fiber is not self.ctx.fiber:
            raise AttributeError(f'cannot set property "{name}" in multiple fibers')
        impl.value = value
        return True

    def provide(
        self,
        name: str,
        value: Any = None,
        check: Callable[[], bool] | None = None,
    ) -> Callable[[], Any]:
        """Port of ``provide``: register a service inside an effect.

        在 effect 中注册服务：effect 存活期间服务可见，effect 销毁时自动注销并
        通知依赖方重新检查。返回的是 effect 的销毁函数。

        注册过程：
        1. 校验 ``props`` 中同名声明不是访问器；
        2. 若根上下文的隔离层尚无该名字，则生成唯一服务键（``Symbol(name)``）；
        3. 若该键已被占用则报错（同一隔离层内服务名必须唯一）；
        4. 写入 ``store`` 与当前 fiber 的 ``provisions``，激活状态下立即通知依赖方；
        5. 注销时先移除绑定并通知，再等待依赖方全部清理完才移除提供记录
           （「先销依赖方、后销提供者」，保证清理期间服务仍可访问）。
        """

        def execute() -> Callable[[], Any]:
            prop = self.props.get(name)
            if prop is not None and prop.type != "service":
                raise TypeError(f'property "{name}" is already declared as {prop.type}')
            self.props[name] = Property(type="service")

            # `this.ctx[symbols.root][symbols.isolate][name] ??= Symbol(name)`
            # 服务键只在根上下文的隔离层中生成一次，后代上下文继承该键
            root = self.ctx.__get_symbol__(symbols.root) or self.ctx
            if root.interface(name) is None:
                root.set_interface(name, service_symbol(name))
            key = self._key(name)
            if key is None:
                raise RuntimeError(f'cannot resolve service key "{name}"')
            existing = self.store.get(key)
            if existing is not None:
                raise RuntimeError(
                    f'service "{name}" has been registered at <{existing.ctx.fiber.name}>'
                )
            impl = Impl(
                uid=next(_IMPL_COUNTER),
                name=name,
                ctx=self.ctx,
                isolate=key,
                value=value,
                check=check,
            )
            self.store[key] = impl
            # provisions 是「本 fiber 提供的服务」集合，供 locate 判定归属
            self.ctx.fiber.provisions.add(impl)
            if self.ctx.fiber.state is FiberState.ACTIVE:
                self.notify([name])

            async def dispose() -> None:
                if self.store.get(impl.isolate) is impl:
                    self.store.pop(impl.isolate, None)
                # 按实现自身的隔离键通知，不随提供者分支后续的 isolate 搬迁漂移
                self.ctx.isolate(name, impl.isolate).reflect.notify([name])
                # Retired bindings cannot gain new dependents. Wait only for
                # their cleanup, even if a fiber has left the registry or
                # starts a new load. Yield even without direct dependents so
                # child cleanup can still access the inherited service before
                # its provision is removed.
                # 已注销的绑定不会再有新依赖方：只需等现有依赖方清理完；即使
                # 没有直接依赖方也让出一步，保证子级清理仍能访问继承的服务。
                await _wait_released(impl)
                impl.release = None
                # ensure self access before dependencies cleanup
                self.ctx.fiber.provisions.discard(impl)

            return dispose

        return self.ctx.fiber.effect(execute, f"ctx.provide({name!r})")

    def notify(
        self,
        names: list[str],
        filter: Callable[[Context, str], bool] | None = None,
    ) -> None:
        """Port of ``notify``: re-check depending fibers and emit the event.

        服务集合变化后的统一处理：
        1. 遍历所有运行中的 fiber，凡是注入过 ``names`` 中某项的（含可选依赖），
           重新检查实现并 ``_refresh()``（必需依赖变化时会触发重载，可选依赖
           只更新可见性不阻塞）；
        2. 对每个名字派发 ``internal/service`` 事件（带过滤上下文，只通知同一
           隔离层的监听者）。

        ``filter`` 默认按隔离键是否相同来判断「是否属于同一可见范围」。
        """
        if filter is None:
            filter = lambda ctx, name: ctx.interface(name) == self.ctx.interface(name)
        for runtime in list(self.ctx.registry.values()):
            for fiber in list(runtime.fibers):
                changed = False
                for name in names:
                    if name not in fiber.inject or not filter(fiber.ctx, name):
                        continue
                    fiber._check_impl(name)
                    changed = True
                if not changed:
                    continue
                fiber._refresh()

        for name in names:
            # `self[symbols.filter] = target => filter(target, name)` then emit
            # 用一个带 filter 协议的临时上下文作为 thisArg，实现隔离过滤
            self_site = self.ctx.extend()
            set_symbol(self_site, symbols.filter, _make_filter(filter, name))
            impl = self._get_impl(name, False)
            self.ctx.events.emit(self_site, "internal/service", name, impl.value if impl else None)

    # -- accessors —— 访问器 -------------------------------------------------
    def accessor(self, name: str, options: PropertyAccessor | AccessorOptions) -> Callable[[], Any]:
        """Port of ``accessor``: ``options`` accepts a mapping or PropertyAccessor.

        注册一个计算属性（只读或可写）。典型用途是 ``mixin`` 把某服务的成员
        以别名暴露到当前上下文上。
        """
        if isinstance(options, PropertyAccessor):
            accessor = options
        else:
            accessor = PropertyAccessor(
                get=cast(AccessorGetter, options.get("get")),
                set=cast(AccessorSetter | None, options.get("set")),
            )

        def execute() -> Callable[[], None]:
            existing = self.props.get(name)
            if existing is not None:
                raise RuntimeError(
                    f'property "{name}" is already declared as {existing.type}'
                )
            self.props[name] = Property(type="accessor", accessor=accessor)

            def dispose() -> None:
                self.props.pop(name, None)

            return dispose

        return self.ctx.fiber.effect(execute, f"ctx.accessor({name!r})")

    def mixin(self, source: Any, mixins: Sequence[str] | Mapping[str, str]) -> Callable[[], Any]:
        """Port of ``mixin``: expose ``source``'s members under new names.

        ``source`` 可以是服务名字符串或服务实例；``mixins`` 可以是
        「名字列表」（原名暴露）或「原名 → 别名」的映射。实现方式是为每个成员
        注册一个 :meth:`accessor`，读时从源服务取，写时写回源服务。
        """
        entries = (
            [(key, key) for key in mixins]
            if isinstance(mixins, Sequence) and not isinstance(mixins, (str, bytes))
            else list(cast(Mapping[str, str], mixins).items())
        )

        def execute() -> list[Callable[[], None]]:
            disposers: list[Callable[[], None]] = []
            for key, exposed in entries:
                def get(ctx: Context, receiver: Any, error: BaseException, key: str = key, source: Any = source) -> Any:
                    service = ctx[source] if isinstance(source, str) else source
                    if is_nullable(service):
                        return service
                    target = with_props(receiver, service) if receiver is not None else service
                    original = getattr(service, "original", service)
                    state = getattr(original, "__dict__", {})
                    if key in state:
                        return state[key]
                    descriptor = _find_descriptor(original, key)
                    if isinstance(descriptor, property):
                        return descriptor.__get__(target, type(original))
                    if isinstance(descriptor, (staticmethod, classmethod)):
                        return descriptor.__get__(target, type(original))
                    if descriptor is not None:
                        if callable(descriptor):
                            return _BoundMixin(descriptor, target)
                        return descriptor
                    return getattr(original, key)

                def set_value(ctx: Context, value: Any, receiver: Any, error: BaseException, key: str = key, source: Any = source) -> bool:
                    service = ctx[source] if isinstance(source, str) else source
                    target = with_props(receiver, service) if receiver is not None else service
                    setattr(target, key, value)
                    return True

                disposers.append(self.accessor(exposed, {"get": get, "set": set_value}))
            return disposers

        return self.ctx.fiber.effect(execute, f"ctx.mixin({source!r})")

    def trace(self, value: ValueT) -> ValueT:
        """把值按需包成追踪代理（``get_traceable`` 的便捷入口）。"""
        return cast(ValueT, get_traceable(self.ctx, value))

    def bind(self, callback: Callable[..., Any]) -> Callable[..., Any]:
        """Port of ``bind``: trace ``thisArg`` and every argument, plus results.

        包一层调用：实参与返回值都经过 :meth:`trace`，使回调内部访问到的对象
        也带有正确的上下文信息（事件监听器的关键前置处理）。
        """
        trace = self.trace

        def bound(*args: Any, **kwargs: Any) -> Any:
            traced_args = tuple(trace(arg) for arg in args)
            result = callback(*traced_args, **kwargs)
            return trace(result)

        return bound


class _BoundMixin:
    """Small helper emulating ``value.bind(mixin)`` for non-function members.

    把可调用成员绑定到 mixin 目标（第一个位置参数传递），近似 JS 的
    ``value.bind(target)``。
    """

    def __init__(self, value: Any, target: Any) -> None:
        self._value = value
        self._target = target

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._value(self._target, *args, **kwargs)

    def __repr__(self) -> str:
        return repr(self._value)


def _find_descriptor(target: Any, name: str) -> Any:
    """沿 MRO 查找类属性描述符（不触发实例 ``__getattr__``）。"""
    for cls in type(target).__mro__:
        if name in vars(cls):
            return vars(cls)[name]
    return None


_REFLECT_TRACKER = None


def _make_filter(filter: Callable[[Context, str], bool], name: str) -> Callable[[Context], bool]:
    """把「(ctx, name) 过滤函数」适配成事件协议要求的「(ctx) 过滤函数」。"""

    def event_filter(target: Context) -> bool:
        return filter(target, name)

    return event_filter


async def _wait_released(impl: Impl) -> None:
    """等待 ``impl`` 的全部依赖方完成清理（无依赖方时也让出一步）。

    依赖方在 ``Fiber._unload`` 末尾把自身从 ``dependents`` 摘除，最后一个
    离开者调用 ``impl.release()`` 唤醒这里。
    """
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def resolve() -> None:
        if not future.done():
            future.set_result(None)

    impl.release = resolve
    if not impl.dependents:
        resolve()
    await future


def _init_tracker() -> None:
    """Deferred init: ``_REFLECT_TRACKER`` must exist once ``Tracker`` is imported.

    延迟初始化：``Tracker`` 定义在本模块依赖的 utils 中，为规避循环导入，
    在模块末尾再创建。
    """
    global _REFLECT_TRACKER
    from .utils import Tracker

    _REFLECT_TRACKER = Tracker(property="ctx", no_shadow=True)


_init_tracker()
