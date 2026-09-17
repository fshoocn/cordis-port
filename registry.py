"""RegistryService and the ``@Inject`` decorator (port of ``core/src/registry.ts``).

中文说明
--------
本模块负责「插件注册表」与「依赖声明」：

* :class:`Runtime` —— 一个插件的运行时记录：插件名、回调、以及由该插件创建的所有
  fiber。同一个插件被加载多次时复用同一 ``Runtime``，只是多挂几个 fiber。
* :class:`RegistryService` —— ``ctx.registry``：解析插件、创建 fiber、维护
  ``插件回调 -> Runtime`` 映射，以及 ``inject`` 语法糖。
* ``@Inject`` 装饰器 —— 声明依赖。类上使用时把依赖合并进 ``inject`` 类属性并标记
  ``symbols.checkProto``；方法上使用时记录元数据并追加 init hook，在插件实例初始化
  时自动调用 ``ctx.inject(...)`` 完成注入。

加载一个插件（``ctx.plugin(p, config)``）的大致流程：

1. :meth:`RegistryService.resolve` 把各种形态的插件（类/函数/带 ``apply`` 的对象/
   字典）统一解析成「回调」；
2. 若该回调尚无 :class:`Runtime`，读取其 ``name``/``inject``/``Config`` 等元信息创建；
3. 用 ``parent.registry.counter`` 分配 uid，创建 :class:`~common.cordis.fiber.Fiber`；
4. fiber 在父 fiber 的 effect 中绑定：解析配置、检查依赖、加载插件本体；
   依赖不满足或配置非法时 fiber 处于非激活状态，等待依赖或配置变化后自动重载。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, ItemsView, KeysView, ValuesView
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, runtime_checkable

from .utils import (
    DisposableList,
    build_outer_stack,
    get_symbol,
    set_symbol,
    symbols,
    with_props,
)

if TYPE_CHECKING:
    from .context import Context
    from .fiber import Fiber

#: 依赖声明形式：名字列表，或「名字 -> 配置」映射
type InjectSpec = list[str] | dict[str, Any | None]
type PluginConfig = Any
#: 插件对外提供的服务名（字符串或字符串列表）
type PluginProvide = str | list[str]
#: 插件拦截声明：服务名 -> 是否拦截
type PluginIntercept = dict[str, bool]
type PluginCallback = Callable[..., Any]
type PluginLike = Any


class StandardSchemaIssue(TypedDict, total=False):
    """Port of ``StandardSchemaV1.Issue`` (``message`` plus optional ``path``).

    标准 Schema 的「校验失败项」：至少含 ``message``，可选 ``path`` 指明出错字段。
    """

    message: str
    path: list[str | int]


@dataclass
class StandardSchemaResult[OutputT]:
    """标准 Schema 校验结果：``value`` 为转换后的值，``issues`` 非空表示失败。"""

    value: OutputT
    issues: list[StandardSchemaIssue] | None = None


@runtime_checkable
class StandardSchemaV1[OutputT](Protocol):
    """Subset of ``@standard-schema/spec`` used by ``resolve_config``.

    只约定 ``validate`` 一个方法；返回 :class:`StandardSchemaResult` 或字典均可
    （``resolve_config`` 两种都支持）。
    """

    def validate(self, value: Any) -> StandardSchemaResult[OutputT] | dict[str, Any]: ...


class PluginBase(Protocol):
    """插件对象/类应具备的元信息（对应官方 ``Plugin`` 接口）。"""

    name: str | None
    Config: StandardSchemaV1[Any] | dict[str, Any] | None
    inject: InjectSpec | None
    provide: PluginProvide | None
    intercept: PluginIntercept | None


class Runtime:
    """Port of TS ``Plugin.Runtime`` (officially lives in ``registry.ts``).

    Canonical home for the runtime record. ``fiber.py`` re-exports this name
    so ``from .fiber import Runtime`` keeps working.

    插件运行时记录：一个插件（回调）对应一个 ``Runtime``，可挂多个 fiber
    （同一插件被加载到多个上下文时）。``fibers`` 为空时该记录会被删除。
    """

    __slots__ = ("Config", "callback", "fibers", "name")

    name: str | None
    callback: Callable[..., Any]
    fibers: DisposableList[Fiber]
    #: 插件的配置校验 Schema（``None`` 表示不校验）
    Config: StandardSchemaV1[Any] | dict[str, Any] | None

    def __init__(
        self,
        name: str | None,
        callback: Callable[..., Any],
        fibers: DisposableList[Fiber],
        Config: StandardSchemaV1[Any] | dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.callback = callback
        self.fibers = fibers
        self.Config = Config

    def __repr__(self) -> str:
        return f"Runtime(name={self.name!r})"


# Back-compat alias: the earlier port exposed a ``PluginRuntime`` Protocol.
PluginRuntime = Runtime


# ---------------------------------------------------------------------------
# @Inject —— 依赖声明装饰器
# ---------------------------------------------------------------------------


def Inject(name: str, config: Any = None) -> Callable[..., Any]:
    """Port of the ``@Inject()`` decorator.

    Official usage relies on TC39 decorators; the Python equivalent decorates a
    class or a method with the same semantics:

    * on a class: merge ``inject`` through the prototype chain and mark it with
      ``symbols.checkProto``;
    * on a method: record metadata and append an init hook that declares the
      dependency and binds ``ctx`` into ``tracker.property`` when calling.

    用法示例::

        class MyPlugin:
            @Inject("database")
            def run(self, ctx): ...      # 依赖 database，注入后自动调用

        @Inject("logger", {"level": 3})
        class OtherPlugin:
            ...

    参数:
        name: 依赖的服务名。
        config: 可选的依赖配置（合并进拦截层，供服务读取）。
    """

    def decorator(value: Any, kind: str | None = None) -> Any:
        resolved_kind = kind or ("class" if isinstance(value, type) else "method")

        if resolved_kind == "class":
            # 类装饰：把依赖合并到 ``inject`` 类属性上，并标记 checkProto
            if not hasattr(value, "inject") or "inject" not in value.__dict__:
                parent_inject = getattr(value, "inject", None)
                inherited: dict[str, Any] = {}
                if isinstance(parent_inject, dict):
                    inherited.update(parent_inject)
                else:
                    # 沿基类查找可继承的 inject（近似 JS 原型的自有属性查找）
                    for base in getattr(value, "__mro__", ())[1:]:
                        base_inject = base.__dict__.get("inject")
                        if isinstance(base_inject, dict):
                            inherited.update(base_inject)
                            break
                value.inject = inherited
                set_symbol(value.inject, symbols.checkProto, True)
            value.inject[name] = config
            return value

        if resolved_kind == "method":
            # 方法装饰：记录元数据 + 追加 init hook
            metadata = get_symbol(value, symbols.metadata)
            if metadata is None:
                metadata = {}
                set_symbol(value, symbols.metadata, metadata)
            inject = metadata.setdefault("inject", {})
            inject[name] = config

            def init_hook(self: Any) -> None:
                """实例初始化时注册依赖，并在依赖就绪后调用被装饰的方法。"""
                tracker = get_symbol(self, symbols.tracker)
                property_name = getattr(tracker, "property", None) if tracker is not None else None

                def callback(ctx: Context, config: Any = None) -> Any:
                    # 若 tracker 指明属性名（如 ``ctx``），把 ctx 通过 withProps 注入，
                    # 使方法内可通过该属性访问上下文
                    if property_name:
                        receiver = with_props(self, {property_name: ctx})
                        return value(self if not hasattr(self, property_name) else receiver)
                    return value(self)

                self.ctx.inject(inject, callback)

            hooks = get_symbol(value, symbols.initHooks)
            if hooks is None:
                hooks = []
                set_symbol(value, symbols.initHooks, hooks)
            hooks.append(init_hook)
            return value

        raise TypeError("@Inject() can only be used on class or class methods")

    return decorator


def resolve_inject(
    inject: InjectSpec | None, result: dict[str, Any | None] | None = None
) -> dict[str, Any | None]:
    """Port of ``Inject.resolve``: flatten through the prototype chain.

    Symbol keys (``symbols.checkProto``) are bookkeeping and never surface as
    dependencies -- JS uses ``defineProperty`` there, Python uses a plain dict,
    so the keys are filtered explicitly.

    把依赖声明归一化成 ``{名字: 配置或 None}``；带 ``checkProto`` 标记时先递归
    合并父级 ``inject``，再叠加自身声明的项。

    注意：``symbols.checkProto`` 只用于标记，不会作为依赖名出现在结果中
    （因此这里显式跳过非字符串键）。
    """
    if result is None:
        result = {}
    if not inject:
        return result
    if isinstance(inject, (list, tuple)):
        # 列表形式：只有名字，无配置
        for name in inject:
            if isinstance(name, str):
                result[name] = None
        return result
    if get_symbol(inject, symbols.checkProto):
        # 需要合并父级声明：先父后子，子级覆盖父级
        parent = None
        if isinstance(inject, dict):
            parent = inject.get("__proto__")
        else:
            bases = getattr(inject, "__mro__", ())
            for base in bases[1:]:
                base_inject = base.__dict__.get("inject")
                if isinstance(base_inject, dict):
                    parent = base_inject
                    break
        if parent is not None:
            resolve_inject(parent, result)
        items = inject.items() if isinstance(inject, dict) else vars(inject).items()
        for name, value in items:
            if isinstance(name, str):
                result[name] = value if value is not None else None
        return result
    items = inject.items() if isinstance(inject, dict) else vars(inject).items()
    for name, value in items:
        if isinstance(name, str):
            result[name] = value if value is not None else None
    return result


@dataclass
class _PluginDefinition:
    """``registry.inject(...)`` 内部使用的插件定义（名字 + 依赖 + 回调）。"""

    name: str | None
    inject: InjectSpec | None
    apply: Callable[..., Any]
    Config: StandardSchemaV1[Any] | dict[str, Any] | None = None
    provide: PluginProvide | None = None
    intercept: PluginIntercept | None = None


class RegistryService:
    """Port of the official ``RegistryService``.

    插件注册表：``ctx.registry``。所有上下文共享同一份 ``_internal`` 映射与计数器，
    因此「同一插件加载到不同上下文」仍复用同一 Runtime。
    """

    def __init__(self, ctx: Context) -> None:
        from .utils import Tracker

        self.ctx = ctx
        self._root = self
        self._counter = 0
        # Port of TS `private _internal = new Map<Function, Plugin.Runtime>()`.
        #: 插件回调 -> Runtime 映射
        self._internal: dict[Callable[..., Any], Runtime] = {}
        # RegistryService 自身无需定义点追踪
        set_symbol(self, symbols.tracker, Tracker(property="ctx", no_shadow=True))

    def for_context(self, ctx: Context) -> RegistryService:
        """派生出面向 ``ctx`` 的同构服务（共享映射与计数器）。"""
        service = object.__new__(type(self))
        service.ctx = ctx
        service._root = self._root
        service._counter = self._root._counter
        service._internal = self._internal
        set_symbol(service, symbols.tracker, get_symbol(self, symbols.tracker))
        return service

    @property
    def counter(self) -> int:
        """自增计数器，用于分配 fiber 的 ``uid``（全局唯一且递增）。"""
        self._root._counter += 1
        return self._root._counter

    @property
    def size(self) -> int:
        """已注册的插件（回调）数量。"""
        return len(self._internal)

    # -- plugin resolution —— 插件解析 ---------------------------------------
    def resolve(self, plugin: PluginLike) -> PluginCallback | None:
        """Port of ``resolve``: plugin.apply may throw, so it is guarded.

        把各种形态的插件统一解析为可调用回调：

        * 字典：取 ``apply``；
        * 类：类本身（由 ``Fiber._run_plugin`` 负责实例化）；
        * 函数/方法：原样返回；
        * 带 ``apply`` 的对象：取该方法；
        * 其他可调用对象：原样返回。

        解析过程中的任何异常都被吞掉（返回 ``None``），对应官方用 try/catch
        保护 ``plugin.apply`` 访问的写法。
        """
        try:
            if isinstance(plugin, dict):
                apply = plugin.get("apply")
                return apply if callable(apply) else None
            if isinstance(plugin, type):
                return plugin
            if inspect.isfunction(plugin) or inspect.ismethod(plugin) or inspect.isbuiltin(plugin):
                return plugin
            if _is_applicable(plugin):
                apply_attr = plugin.apply
                return apply_attr if callable(apply_attr) else None
            if callable(plugin):
                return plugin
        except Exception:  # noqa: BLE001
            return None
        return None

    def get(self, plugin: PluginLike) -> Runtime | None:
        """取插件对应的 :class:`Runtime`；未注册返回 ``None``。"""
        key = self.resolve(plugin)
        if key is None:
            return None
        return self._internal.get(key)

    def has(self, plugin: PluginLike) -> bool:
        """插件是否已注册。"""
        key = self.resolve(plugin)
        return key is not None and key in self._internal

    def delete(self, plugin: PluginLike) -> Runtime | None:
        """注销插件：移除 Runtime 并销毁其所有 fiber。"""
        key = self.resolve(plugin)
        if key is None:
            return None
        runtime = self._internal.get(key)
        if runtime is None:
            return None
        self._internal.pop(key, None)
        for fiber in list(runtime.fibers):
            fiber.dispose()
        return runtime

    def keys(self) -> KeysView[PluginCallback]:
        return self._internal.keys()

    def values(self) -> ValuesView[Runtime]:
        return self._internal.values()

    def entries(self) -> ItemsView[PluginCallback, Runtime]:
        return self._internal.items()

    def for_each(self, callback: Callable[[Runtime, PluginCallback], Any]) -> None:
        """Port of ``forEach(value, key)``（注意参数顺序：先值后键）。"""
        for key, value in list(self._internal.items()):
            callback(value, key)

    # ``forEach`` alias matching the official spelling
    forEach = for_each

    def inject(self, inject: InjectSpec, callback: PluginCallback) -> Fiber:
        """Port of ``inject``: sugar for ``plugin({ inject, apply, name })``.

        语法糖：以「只依赖某个服务」的方式加载一个回调（回调名作为插件名）。
        """
        return self.plugin(
            _PluginDefinition(
                name=getattr(callback, "__name__", None),
                inject=inject,
                apply=callback,
            )
        )

    def plugin(
        self,
        plugin: PluginLike,
        config: PluginConfig = None,
        get_outer_stack: Callable[[], list[str]] | None = None,
    ) -> Fiber:
        """Port of ``plugin``: create or reuse a runtime, then a fiber.

        加载插件的主入口：

        1. 解析插件为回调（失败即 ``TypeError``）；
        2. 校验当前 fiber 处于激活状态（不允许在已卸载上下文上加载）；
        3. 按回调查找/创建 :class:`Runtime`（同名回调复用，因此可多次加载）；
        4. 归一化依赖声明，创建 :class:`~common.cordis.fiber.Fiber`。

        返回的 fiber 可直接 ``await``（``Fiber.__await__``），用于等待插件加载完成。
        """
        callback = self.resolve(plugin)
        if callback is None:
            raise TypeError(
                "invalid plugin, expect function or object with an "
                f'"apply" method, received {type(plugin).__name__}'
            )
        self.ctx.fiber.assert_active()

        runtime = self._internal.get(callback)
        if runtime is None:
            name = _plugin_name(plugin)
            if name == "apply":
                # 插件是「带 apply 的对象」时，``name`` 会取到方法名 "apply"，
                # 这不是有意义的插件名，置空以回退到类名/函数名
                name = None
            runtime = Runtime(name, callback, DisposableList(), _plugin_config(plugin))
            self._internal[callback] = runtime

        from .fiber import Fiber

        inject = resolve_inject(_plugin_inject(plugin))
        fiber = Fiber(
            self.ctx,
            config,
            inject,
            runtime,
            get_outer_stack or build_outer_stack(),
        )
        # the official returns `Object.create(fiber)` with a `then` method so the
        # fiber can be awaited directly, which `Fiber.__await__` already provides
        return fiber


def _is_applicable(obj: Any) -> bool:
    """Port of ``isApplicable``：是否为「带 ``apply`` 方法的对象」。

    注意类被显式排除：类作为插件时由 ``Fiber._run_plugin`` 直接实例化，
    而不是取它的 ``apply``。
    """
    if obj is None:
        return False
    if isinstance(obj, dict):
        return callable(obj.get("apply"))
    if isinstance(obj, type):
        return False
    return callable(getattr(obj, "apply", None))


def _plugin_name(plugin: PluginLike) -> str | None:
    """``plugin.name`` with a graceful fallback for plain Python functions.

    取插件名：优先 ``name``，回退 ``__name__``（普通函数/类）。
    """
    if isinstance(plugin, dict):
        return plugin.get("name")
    name = getattr(plugin, "name", None)
    if name is None:
        name = getattr(plugin, "__name__", None)
    return name


def _plugin_config(plugin: PluginLike) -> StandardSchemaV1[Any] | dict[str, Any] | None:
    """取插件声明的配置 Schema（``Config`` 属性或字典键）。"""
    if isinstance(plugin, dict):
        return plugin.get("Config")
    return getattr(plugin, "Config", None)


def _plugin_inject(plugin: PluginLike) -> InjectSpec | None:
    """取插件声明的依赖（``inject`` 属性或字典键）。"""
    if isinstance(plugin, dict):
        return plugin.get("inject")
    return getattr(plugin, "inject", None)
