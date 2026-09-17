"""Context: the central facade of cordis (port of ``packages/core/src/context.ts``).

The official implementation returns a ``Proxy`` from its constructor, so every
property access flows through ``ReflectService.handler``.  Python has no such
hook, so :class:`Context` implements the same dispatch in ``__getattr__`` /
``__setattr__`` / ``__contains__`` / ``__setitem__`` and keeps the plugin-facing
API identical.

中文说明
--------
``Context`` 是插件的「全局门面」：插件只拿到一个 ``ctx`` 就能访问服务、注册
事件、加载子插件、读取配置。

官方的 ``Context`` 构造时返回 ``Proxy``，所有属性读写都经过 ``ReflectService``
的处理器；Python 没有等价钩子，因此这里把同样的分派逻辑放进 ``__getattr__`` /
``__setattr__`` / ``__contains__`` / ``__setitem__``，保持插件可见的 API 一致。

几个关键设计：

* **继承（extend）**：子上下文通过 ``__proto__`` 链接到父上下文，服务与符号查找
  沿链上溯，因此子上下文天然能访问父级注册的服务。
* **隔离（isolate）**：``ctx.isolate('foo')`` 为名字生成新的「隔离键」，使该分支
  下看到的 ``foo`` 与其他分支互不相同（多实例服务的实现基础）。
* **拦截（intercept）**：``ctx.intercept('foo', config)`` 为名字叠加配置层，优先级
  高于外层，供服务读取自己那份被覆盖的配置。
* **属性分派**：读取未命中的名字时不直接报错，而是交给 ``reflect.handler_get``
  走「服务解析」流程（详见 :mod:`common.cordis_port.reflect`）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, TypeGuard

from .utils import ProtoDict, _Symbol, get_symbol, has_symbol, symbols

if TYPE_CHECKING:
    from .events import EventName, EventOptionsInput, EventsService
    from .fiber import Disposable, Effect, Fiber
    from .logger import LoggerService
    from .reflect import AccessorOptions, PropertyAccessor, ReflectService
    from .registry import (
        InjectSpec,
        PluginCallback,
        PluginConfig,
        PluginLike,
        RegistryService,
    )


class _ContextSymbolMethod:
    """Expose an official symbol on the class and a Python method on instances.

    描述符：使 ``Context.effect`` 在类上访问时得到「官方符号对象」，
    在实例上访问时得到「绑定好的方法」（即 ``ctx.effect(...)`` 可用）。
    这样既保住了 ``Context.effect === symbols.effect`` 的官方语义，
    又提供了符合 Python 习惯的调用方式。
    """

    def __init__(self, symbol: _Symbol, method_name: str) -> None:
        self.symbol = symbol
        self.method_name = method_name

    def __get__(
        self, instance: Context | None, owner: type[Context]
    ) -> _Symbol | Callable[..., Any]:
        if instance is None:
            # 类访问：返回官方符号
            return self.symbol
        # 实例访问：返回实际方法
        return getattr(instance, self.method_name)


class Context:
    """Port of the official ``Context`` class.

    插件门面对象。``ctx`` 上的成员大致分四类：

    * 服务：``ctx.logger``、``ctx.events``、``ctx.registry``、``ctx.reflect``；
    * 快捷方法：``ctx.on`` / ``ctx.emit`` / ``ctx.plugin`` / ``ctx.provide`` 等
      （由 ``ReflectService._bootstrap`` 的 mixin 挂上，实际转发到对应服务）；
    * 数据：``ctx.foo``（自定义服务或属性）、``ctx[symbol]``（符号槽位）；
    * 生命周期：``ctx.fiber``、``ctx.root``、``ctx.parent``。
    """

    # official static symbols —— 官方静态符号（类上访问得符号，实例上访问得方法）
    effect = _ContextSymbolMethod(symbols.effect, "_effect")
    filter = symbols.filter
    isolate = _ContextSymbolMethod(symbols.isolate, "_isolate")
    intercept = _ContextSymbolMethod(symbols.intercept, "_intercept")

    # snake_case aliases kept for the Python API —— Python 风格别名
    effect_key = symbols.effect
    filter_key = symbols.filter
    isolate_key = symbols.isolate
    intercept_key = symbols.intercept

    root: Context
    baseUrl: str | None
    fiber: Fiber
    reflect: ReflectService
    registry: RegistryService
    events: EventsService
    logger: LoggerService

    _symbols: dict[_Symbol, Any]
    _parent: Context | None

    def __init__(self) -> None:
        from .events import EventsService
        from .fiber import Fiber
        from .logger import LoggerService
        from .reflect import ReflectService
        from .registry import RegistryService

        # 符号槽位：isolate / intercept 两层初始为空（都是带原型的字典）
        self._symbols = {
            symbols.isolate: ProtoDict(),
            symbols.intercept: ProtoDict(),
        }
        self._parent = None
        self.root = self
        self.baseUrl = None
        self.base_url = None

        # 根 fiber：无 runtime，状态恒为 ACTIVE，承载所有全局服务
        self.fiber = Fiber(self, {}, {}, None)
        self.reflect = ReflectService(self)
        self.registry = RegistryService(self)
        self.events = EventsService(self)
        self.logger = LoggerService(self)
        # 上面各服务构造时会把自身 effect 登记到 fiber 上，根 fiber 不需要它们
        self.fiber._disposables.clear()

    # -- symbol table —— 符号表 ----------------------------------------------
    def __get_symbol__(self, symbol: _Symbol, default: Any = None) -> Any:
        """读符号：自身没有则沿 ``_parent`` 链向上找（上下文继承）。"""
        if symbol in self._symbols:
            return self._symbols[symbol]
        parent = self.__dict__.get("_parent")
        if parent is not None:
            return parent.__get_symbol__(symbol, default)
        return default

    def __set_symbol__(self, symbol: _Symbol, data: Any) -> None:
        """写符号：只写在当前上下文（不污染父级）。"""
        self._symbols[symbol] = data

    def __has_symbol__(self, symbol: _Symbol) -> bool:
        """符号是否存在（含父级链）。"""
        if symbol in self._symbols:
            return True
        parent = self.__dict__.get("_parent")
        return parent is not None and parent.__has_symbol__(symbol)

    # -- protocol —— 类型协议 ------------------------------------------------
    is_context = True
    _cordis_cordis_is_context = True

    @staticmethod
    def is_(value: Any) -> TypeGuard[Context]:
        """Port of ``Context.is``: ``!!value?.[Context.is]``.

        官方类型判定：对象自报 ``is_context`` 即为上下文（鸭子类型），
        因此代理、包装对象也能通过判定。
        """
        return bool(getattr(value, "is_context", False))

    @staticmethod
    def is_instance(value: Any) -> bool:
        """严格一些的判定：真实 ``Context`` 实例或自报 ``is_context`` 的对象。"""
        return isinstance(value, Context) or bool(getattr(value, "is_context", False))

    def extend(self, meta: Mapping[str | _Symbol, Any] | None = None) -> Context:
        """Port of ``extend``: create a child carrying the same services.

        创建子上下文：

        * 字符串键写入 ``__dict__``（普通属性）；
        * 符号键写入符号表；
        * 四个服务派生新实例（共享底层存储），``fiber`` 复用父级；
        * 建立 ``__proto__`` 链接，使符号查找与属性回退沿链上溯。

        ``meta`` 中的 ``{"fiber": self}`` 是常见用法：让子上下文的 ``ctx.fiber``
        指向新 fiber。
        """
        from .utils import get_proto, set_proto

        child = object.__new__(type(self))
        child.__dict__["_symbols"] = {}
        child.__dict__["_parent"] = self
        child.__dict__["_proto"] = self
        child.__dict__["root"] = self.root
        child.__dict__["baseUrl"] = self.baseUrl
        child.__dict__["base_url"] = self.baseUrl
        child.__dict__["reflect"] = self.reflect.for_context(child)
        child.__dict__["registry"] = self.registry.for_context(child)
        child.__dict__["events"] = self.events.for_context(child)
        child.__dict__["logger"] = self.logger.for_context(child)
        child.__dict__["fiber"] = self.fiber
        for name, value in (meta or {}).items():
            if not isinstance(name, str):
                child.__set_symbol__(name, value)
            else:
                child.__dict__[name] = value
        set_proto(child, self)
        # keep the official prototype semantics for symbol lookups too
        assert get_proto(child) is self
        return child

    def _isolate(self, name: str, label: _Symbol | None = None) -> Context:
        """Port of ``isolate``: shadow ``name`` with ``label`` for this branch.

        为 ``name`` 在当前分支生成独立隔离键（未给出 ``label`` 时新建唯一符号），
        从而让该分支看到的服务实例与其他分支隔离——同名多实例的基础。
        """
        from .utils import _new_symbol

        current = self.__get_symbol__(symbols.isolate, None)
        if not isinstance(current, ProtoDict):
            current = ProtoDict(mapping=current)
        # 新层继承旧层，只覆盖 name 一项：其余名字继续沿用外层隔离键
        shadow = ProtoDict(proto=current)
        shadow[name] = label if label is not None else _new_symbol(f"cordis.isolate.{name}")
        return self.extend({symbols.isolate: shadow})

    def _intercept(self, name: str, config: Any) -> Context:
        """Port of ``intercept``: stack a config layer for ``name``.

        为 ``name`` 叠加一层配置（``config``）：越靠近当前上下文的层优先级越高。
        服务可通过 ``ctx.resolveConfig`` 读到自己那份被覆盖的配置。
        """
        current = self.__get_symbol__(symbols.intercept, None)
        if not isinstance(current, ProtoDict):
            current = ProtoDict(mapping=current)
        intercept = ProtoDict(proto=current)
        intercept[name] = config
        return self.extend({symbols.intercept: intercept})

    # -- reflect facade —— 反射门面 ------------------------------------------
    def get(self, name: str, strict: bool = True, **kwargs: Any) -> Any:
        """取服务值；``receiver`` 存在时走「带接收者的代理读取」路径。"""
        receiver = kwargs.get("receiver")
        if receiver is not None:
            return self.reflect.handler_get(
                self.extend({symbols.receiver: receiver}),
                name,
            )
        return self.reflect.get(name, strict, **kwargs)

    def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        """写服务值（仅提供者 fiber 可写）；``receiver`` 语义同 :meth:`get`。"""
        receiver = kwargs.get("receiver")
        if receiver is not None:
            return self.reflect.handler_set(
                self.extend({symbols.receiver: receiver}),
                name,
                value,
            )
        return self.reflect.set(name, value, **kwargs)

    def provide(
        self,
        name: str,
        value: Any = None,
        check: Callable[[], bool] | None = None,
    ) -> Disposable[Any]:
        """注册服务（effect 生命周期内有效），返回注销函数。"""
        return self.reflect.provide(name, value, check)

    def accessor(
        self, name: str, options: PropertyAccessor | AccessorOptions
    ) -> Disposable[Any]:
        """注册计算属性（只读/可写），返回注销函数。"""
        return self.reflect.accessor(name, options)

    def mixin(
        self, source: Any, mixins: Sequence[str] | Mapping[str, str]
    ) -> Disposable[Any]:
        """把某服务的成员以别名暴露到当前上下文。"""
        return self.reflect.mixin(source, mixins)

    def _effect(
        self,
        execute: Callable[[], Effect[Any] | None],
        label: str = "anonymous",
    ) -> Disposable[Any]:
        """创建副作用（对应 ``ctx.effect``，fiber 卸载时自动清理）。"""
        return self.fiber.effect(execute, label)

    # -- events —— 事件（转发到 events 服务） --------------------------------
    def on(
        self,
        name: EventName,
        listener: Callable[..., Any],
        options: EventOptionsInput = None,
    ) -> Disposable[Any]:
        return self.events.on(name, listener, options)

    def once(
        self,
        name: EventName,
        listener: Callable[..., Any],
        options: EventOptionsInput = None,
    ) -> Disposable[Any]:
        return self.events.once(name, listener, options)

    def emit(self, *args: Any) -> None:
        return self.events.emit(*args)

    async def parallel(self, *args: Any) -> None:
        return await self.events.parallel(*args)

    async def serial(self, *args: Any) -> Any:
        return await self.events.serial(*args)

    def bail(self, *args: Any) -> Any:
        return self.events.bail(*args)

    def waterfall(self, *args: Any) -> Any:
        return self.events.waterfall(*args)

    def dispatch(self, mode: str, args: list[Any]) -> list[Callable[..., Any]]:
        return self.events.dispatch(mode, args)

    # -- registry —— 插件（转发到 registry 服务） ----------------------------
    def plugin(self, plugin: PluginLike, config: PluginConfig = None) -> Fiber:
        return self.registry.plugin(plugin, config)

    def inject(self, inject: InjectSpec, callback: PluginCallback) -> Fiber:
        return self.registry.inject(inject, callback)

    # -- fiber —— 生命周期（转发到 fiber） -----------------------------------
    async def update(self, config: Any, no_save: bool = False) -> Any:
        return await self.fiber.update(config, no_save)

    async def restart(self) -> None:
        return await self.fiber.restart()

    def assert_active(self) -> None:
        self.fiber.assert_active()

    # -- dict-like access —— 字典式访问 --------------------------------------
    def __contains__(self, name: object) -> bool:
        """``name in ctx``：自有属性 → 服务仓库 → 父级链。"""
        if not isinstance(name, str):
            return False
        if name in self.__dict__:
            return True
        reflect = self.__dict__.get("reflect")
        if reflect is not None and (reflect.has(name) or name in reflect.props):
            return True
        parent = self.__dict__.get("_parent")
        if parent is not None:
            return name in parent
        return False

    def __getitem__(self, name: str | _Symbol) -> Any:
        """``ctx[name]``：等价于属性读取（含符号键）。"""
        return self.get_owned(name)

    def __setitem__(self, name: str | _Symbol, value: Any) -> None:
        """``ctx[name] = value``：字符串走属性分派，符号写符号表。"""
        if isinstance(name, str):
            setattr(self, name, value)
        else:
            self.__set_symbol__(name, value)

    def get_owned(self, name: str | _Symbol) -> Any:
        """Port of ``handler.get``: resolve ``ctx[name]`` without attribute magic.

        解析 ``ctx[name]``：字符串走 ``__getattr__``（即服务解析流程），
        符号走符号表查找。特殊属性判断保留以对齐官方分支结构。
        """
        from .reflect import is_special_property

        if is_special_property(name):
            return getattr(self, name) if isinstance(name, str) else self.__get_symbol__(name)
        return getattr(self, name) if isinstance(name, str) else self.__get_symbol__(name)

    # -- attribute dispatch (the Proxy handler) —— 属性分派（官方 Proxy 处理器）
    def __getattr__(self, name: str) -> Any:
        """读取未命中属性时进入服务解析流程。

        ``reflect`` 尚未初始化（构造过程中）时直接抛 ``AttributeError``，
        避免递归；``__dunder__`` 同样直接抛出（解释器内部探测）。
        """
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        reflect = self.__dict__.get("reflect")
        if reflect is None:
            raise AttributeError(name)
        return reflect.handler_get(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """写入属性：``_`` 开头走原生 ``object.__setattr__``，其余进入服务分派。"""
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        reflect = self.__dict__.get("reflect")
        fiber = self.__dict__.get("fiber")
        if reflect is None or fiber is None:
            # 构造早期（reflect 未就绪）退化为普通属性写入
            object.__setattr__(self, name, value)
            return
        reflect.handler_set(self, name, value)

    # -- niceties —— 便捷访问 -------------------------------------------------
    @property
    def parent(self) -> Context | None:
        """父上下文（根上下文为 ``None``）。"""
        return self.__dict__.get("_parent")

    def is_isolated(self, name: str) -> bool:
        """名字在当前分支是否被隔离（``isolate`` 覆盖过）。"""
        return name in (self.__get_symbol__(symbols.isolate, {}) or {})

    def interface(self, name: str) -> _Symbol | None:
        """Port of ``this[symbols.isolate][name]`` (walks the prototype chain).

        取名字对应的隔离键；沿隔离层原型链查找，找不到返回 ``None``。
        该键用于比较「两个上下文看到的是不是同一个服务」。
        """
        isolate = self.__get_symbol__(symbols.isolate, None)
        if isinstance(isolate, ProtoDict):
            return isolate.get(name)
        if isinstance(isolate, dict):
            return isolate.get(name)
        return None

    def set_interface(self, name: str, key: _Symbol) -> None:
        """Port of ``this[symbols.isolate][name] = key``.

        设置名字的隔离键（``reflect.provide`` 首次注册服务时调用）。
        """
        isolate = self.__get_symbol__(symbols.isolate, None)
        if not isinstance(isolate, ProtoDict):
            isolate = ProtoDict(mapping=isolate if isinstance(isolate, dict) else None)
            self.__set_symbol__(symbols.isolate, isolate)
        isolate[name] = key

    def __eq__(self, other: object) -> bool:
        """相等性：同一对象，或共享同一 ``__dict__``（``extend`` 产生的视图）。"""
        if self is other:
            return True
        if isinstance(other, Context):
            return self.__dict__ is other.__dict__
        return NotImplemented

    def __hash__(self) -> int:
        return id(self.__dict__)

    def __repr__(self) -> str:
        fiber = self.__dict__.get("fiber")
        name = getattr(fiber, "name", "root")
        return f"Context <{name}>"


def is_context(value: Any) -> bool:
    """Port of ``Context.is`` (kept as a module-level helper too).

    模块级类型判定：先看 ``symbols.is_context`` 符号槽位，再回退到
    ``isinstance`` / ``is_context`` 属性。
    """
    if has_symbol(value, symbols.is_context):
        return bool(get_symbol(value, symbols.is_context))
    return isinstance(value, Context) or bool(getattr(value, "is_context", False))
