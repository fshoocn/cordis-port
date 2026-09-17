"""Service base class (port of ``packages/core/src/service.ts``).

Registered through ``ctx.reflect.provide`` and carrying a ``Tracker`` so that
``ctx.foo.bar`` / ``ctx['foo.bar']`` reach the service's own members.

The official class implements symbol-keyed members (``[symbols.invoke]``,
``[symbols.filter]`` ...).  Python has no symbol keys, so subclasses define the
dunder methods below and :mod:`common.cordis.utils` maps the protocol symbols
onto them:

===========================  ==========================
official symbol              Python method
===========================  ==========================
``symbols.invoke``           ``__invoke__(self, *args)``
``symbols.filter``           ``__filter__(self, ctx)``
``symbols.extend``           ``__extend__(self, props)``
``symbols.resolveConfig``    ``__resolve_config__(self, base, head)``
``symbols.check``            ``__check__(self)``
``symbols.init``             ``__init_service__(self)``
``symbols.config``           ``Config`` class attribute
===========================  ==========================

中文说明
--------
``Service`` 是所有「服务」的基类：任何希望通过 ``ctx.xxx`` 被插件访问的能力
（日志、事件、数据库连接……）都应该继承它。

核心机制：

1. **注册**：构造时调用 ``ctx.reflect.provide(name, self, check)``，把自己登记
   到 ``ctx.reflect.store`` 中，键是该服务名对应的符号（symbol）。
2. **依赖追踪**：每个服务携带一个 :class:`~common.cordis.utils.Tracker`，其中
   ``property="ctx"``、``associate=服务名``。这样 ``ctx.foo.bar`` 会被解析为
   ``ctx.foo``（拿到服务实例）后再取 ``bar``，而 ``ctx['foo.bar']`` 这种带点
   的写法则由 ``tracker.associate`` 兜底解析。
3. **协议映射**：官方 TS 使用符号键成员（``[symbols.invoke]`` 等），Python 没有
   符号键语法，因此由 :mod:`common.cordis.utils` 的 ``register_protocol`` 把每个
   符号映射到一个 dunder 方法上（见上表），子类只要实现对应 dunder 即可。
4. **可调用服务**：实现了 ``__invoke__`` 的服务可以像函数一样调用，例如
   ``ctx.logger('name')``。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from .utils import (
    ProtoDict,
    Tracker,
    get_symbol,
    protocol_attr,
    register_protocol,
    set_symbol,
    symbols,
)

if TYPE_CHECKING:
    from .context import Context

ConfigT = TypeVar("ConfigT")

# 把「协议符号」绑定到对应的 Python dunder 方法：
# 之后 get_symbol(service, symbols.invoke) 找不到实例槽位时，会自动回退到
# service.__invoke__，从而让子类只实现 dunder 就满足官方协议。
register_protocol(symbols.invoke, "__invoke__")
register_protocol(symbols.filter, "__filter__")
register_protocol(symbols.extend, "__extend__")
register_protocol(symbols.resolveConfig, "__resolve_config__")
register_protocol(symbols.check, "__check__")
register_protocol(symbols.init, "__init_service__")


class Service(Generic[ConfigT]):
    """Port of the official abstract ``Service`` class.

    服务基类。子类通常只需声明类属性 ``provide``（服务名）与 ``Config``（配置
    模型），再在 ``__init_service__`` 中完成初始化逻辑即可。
    """

    # 官方静态符号（命名与 TS 版 ``Service.invoke`` 等保持一致，便于对照阅读）
    init = symbols.init
    check = symbols.check
    config = symbols.config
    invoke = symbols.invoke
    extend = symbols.extend
    tracker = symbols.tracker
    resolveConfig = symbols.resolveConfig

    # 以下为 Python 风格的下划线别名，与上面的名称指向同一符号对象
    init_key = symbols.init
    check_key = symbols.check
    config_key = symbols.config
    invoke_key = symbols.invoke
    extend_key = symbols.extend
    tracker_key = symbols.tracker
    resolve_config_key = symbols.resolveConfig

    name: str
    ctx: Context
    #: 配置模型；若它定义了 ``merge`` 方法，``__resolve_config__`` 会优先调用它
    Config: Any = None
    #: 服务名；子类可直接写 ``provide = "foo"``，无需在构造时显式传 name
    provide: str | None = None

    def __init__(self, ctx: Context, name: str | None = None) -> None:
        """构造服务并立即注册到 ``ctx.reflect``。

        参数:
            ctx: 服务所属的上下文（通常是某插件 fiber 的 ctx）。
            name: 服务名；省略时回退到类属性 ``provide``。

        抛出:
            ValueError: 既未传入 ``name``，类上也没有 ``provide`` 时。
        """
        # 服务名解析顺序：显式参数 > 类属性 ``provide``
        resolved_name = name or getattr(type(self), "provide", None)
        if not resolved_name:
            raise ValueError("service name is required")

        # associate=服务名：让 ``ctx['服务名.成员']`` 这类带点访问能被正确路由；
        # property="ctx"：表明服务实例由上下文的 ``ctx`` 属性持有。
        tracker = Tracker(associate=resolved_name, property="ctx")
        self.ctx = ctx
        self.name = resolved_name
        set_symbol(self, symbols.tracker, tracker)

        # 解析「服务是否可用」的检查函数（官方 ``[symbols.check]``），随服务一同登记；
        # 注入方在绑定依赖时会调用它，决定服务此刻是否真的可用。
        check = self._resolve_check()
        set_symbol(self, symbols.check, check)
        ctx.reflect.provide(resolved_name, self, check)

    # -- 可调用服务 ----------------------------------------------------------
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Port of ``applyTraceable`` dispatching to ``self[symbols.invoke]``.

        让服务实例可以像函数一样被调用（例如 ``ctx.logger('name')``）：
        实际转发到子类实现的 ``__invoke__``；未实现时抛出 ``TypeError``，
        对应官方「该服务不可调用」的行为。
        """
        invoke = getattr(type(self), "__invoke__", None)
        if invoke is None:
            raise TypeError(f"service {self.name!r} is not callable")
        return invoke(self, *args, **kwargs)

    def _resolve_check(self) -> Callable[[], bool] | None:
        """解析服务的可用性检查函数（官方 ``[symbols.check]``）。

        查找顺序：
        1. 子类重写的 ``__check__``；
        2. 子类的 ``check_method``（Python 风格别名，避免与实例属性 ``check`` 冲突）；
        3. 实例 ``__dict__`` 中的 ``check`` 字段（例如构造完成后动态赋值）。
        都找不到时返回 ``None``，表示「无额外检查」。
        """
        check = getattr(type(self), "__check__", None)
        if check is None or check is Service.__check__:
            # 基类默认实现没有实际意义，改试 Python 风格别名
            check = getattr(type(self), "check_method", None)
        if check is None:
            check = self.__dict__.get("check")
        if callable(check) and check is not self:
            return cast(Callable[[], bool], check)
        return None

    # -- 协议方法（对应官方的符号键成员） ------------------------------------
    def __filter__(self, ctx: Context) -> bool:
        """Port of ``[symbols.filter]``.

        事件过滤协议：仅当访问方与服务注册方处于同一隔离层（``isolate``）时返回
        ``True``，从而避免事件跨隔离边界泄漏。
        """
        return ctx.interface(self.name) == self.ctx.interface(self.name)

    def __extend__(self, props: Any = None) -> Any:
        """Port of ``[symbols.extend]``.

        派生一个「带附加属性」的服务视图，对应官方 ``withProps`` 语义：

        * 可调用服务：经 :func:`create_callable` 生成新的可调用对象；
        * 普通服务：复制实例字典后覆盖属性。
        """
        from .utils import create_callable

        if getattr(type(self), "__invoke__", None) is not None:
            # 可调用服务：构造一个仍可调用、且携带 tracker 的新对象
            self_value: Any = create_callable(
                self.name, self, get_symbol(self, symbols.tracker)
            )
        else:
            self_value = object.__new__(type(self))
            self_value.__dict__.update(self.__dict__)
        for key, value in (props or {}).items():
            setattr(self_value, key, value)
        return self_value

    def __resolve_config__(
        self, base: ConfigT | None = None, head: ConfigT | None = None
    ) -> ConfigT:
        """Port of ``[symbols.resolveConfig]``.

        按「拦截层（intercept）→ base → 服务自身拦截配置 → head」的顺序合并配置：

        * 拦截层取自 ``ctx[symbols.intercept]`` 的原型链，``insert(0)`` 保证越靠近
          当前上下文的层优先级越高（后写入者覆盖先写入者）；
        * 若 ``Config`` 定义了 ``merge`` 方法则交由它合并，否则退化为浅层字典 update。
        """
        intercept = self.ctx.__get_symbol__(symbols.intercept, ProtoDict())
        configs: list[Any] = []
        layers = intercept.chain() if isinstance(intercept, ProtoDict) else [intercept]
        # 逐层收集本服务名对应的配置，越靠当前层优先级越高
        for layer in layers:
            if self.name in layer:
                configs.insert(0, layer[self.name])
        if base is not None:
            configs.insert(0, base)
        if head is not None:
            configs.append(head)
        # 优先使用配置模型自带的 merge（例如 cosmokit 的 Config.merge）
        merge = getattr(getattr(type(self), "Config", None), "merge", None)
        if callable(merge):
            return cast(ConfigT, merge(*configs))
        result: dict[str, Any] = {}
        for config in configs:
            if isinstance(config, dict):
                result.update(config)
        return cast(ConfigT, result)

    # -- 基类协议默认实现（子类可覆盖） --------------------------------------
    def __check__(self) -> bool:
        """Default ``[symbols.check]``: always available.

        默认表示「服务始终可用」；子类可重写为依赖就绪状态的动态判断。
        """
        return True

    # -- 符号读写 ------------------------------------------------------------
    def __get_symbol__(self, symbol: Any, default: Any = None) -> Any:
        """自定义符号查找：实例字典 → ``__proto__`` 原型链 → 协议 dunder。

        这让 :func:`common.cordis.utils.get_symbol` 对服务实例也能正常工作，
        与官方基于原型链的符号继承语义保持一致。
        """
        state = getattr(self, "__dict__", {})
        attr = "_cordis_" + symbol.name.replace(".", "_")
        if attr in state:
            return state[attr]
        # 沿 ``__proto__`` 链向上查找（``Service.__extend__`` 会保留该链接）
        proto = state.get("__proto__") if isinstance(state, dict) else None
        if proto is not None:
            value = get_symbol(proto, symbol, None)
            if value is not None:
                return value
        # 最后按协议映射回退到 dunder 方法
        protocol = protocol_attr(symbol)
        if protocol is not None:
            method = getattr(self, protocol, None)
            if callable(method):
                return method
        return default

    def __set_symbol__(self, symbol: Any, data: Any) -> None:
        """写入符号：统一存成 ``_cordis_<符号名>`` 形式的实例属性。"""
        self.__dict__["_cordis_" + symbol.name.replace(".", "_")] = data

    def __getitem__(self, name: Any) -> Any:
        """``service[symbol_or_name]`` 读取语法糖。"""
        return get_symbol(self, name)

    def __setitem__(self, name: Any, value: Any) -> None:
        """``service[symbol_or_name] = value`` 写入语法糖。"""
        set_symbol(self, name, value)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={getattr(self, 'name', '?')!r}>"


def is_service_instance(value: Any) -> bool:
    """Port of the official ``Symbol.hasInstance``.

    判断 ``value`` 是否为服务实例；若传入的是追踪代理（proxy），则递归检查其
    ``symbols.original`` 指向的原始对象。
    """
    if value is None:
        return False
    if isinstance(value, Service):
        return True
    original = get_symbol(value, symbols.original)
    if original is not None and original is not value:
        return is_service_instance(original)
    return False
