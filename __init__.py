"""Cordis: a Python port of ``cordiverse/cordis`` ``packages/core``.

Public surface mirrors the official ``index.ts`` (``export *`` from every
module), with camelCase aliases kept alongside the Pythonic names.

中文说明
--------
``common.cordis_port`` 是 cordiverse/cordis（TS 版）核心包 ``packages/core`` 的
Python 移植，实现了「插件化 + 依赖注入 + 事件总线」的运行时框架。

模块结构（对应官方同名文件）：

=========================  ================================================
模块                        职责
=========================  ================================================
:mod:`~common.cordis_port.context`    ``Context``：插件门面（服务访问入口）
:mod:`~common.cordis_port.fiber`      ``Fiber``：插件生命周期与副作用调度
:mod:`~common.cordis_port.registry`   插件注册表与 ``@Inject`` 依赖声明
:mod:`~common.cordis_port.reflect`    服务仓库与属性访问路由（依赖注入核心）
:mod:`~common.cordis_port.events`     事件总线（五种派发模式）
:mod:`~common.cordis_port.logger`     日志服务与格式化/导出
:mod:`~common.cordis_port.service`    服务基类
:mod:`~common.cordis_port.utils`      基础设施（符号、原型链、追踪代理）
=========================  ================================================

本包同时导出两套命名：Python 风格的 ``snake_case`` 与官方 TS 的 ``camelCase``
别名，二者指向同一对象，方便对照官方文档使用。

最简用法::

    from common.cordis_port import Context

    ctx = Context()

    class MyPlugin:
        inject = ["logger"]          # 声明依赖 logger

        def __init__(self, ctx, config):
            self.ctx = ctx

        def init(self):
            self.ctx.logger.info("plugin loaded")

    await ctx.plugin(MyPlugin, {"foo": 1})
"""

from .context import Context, is_context
from .events import (
    EventDispatchError,
    EventName,
    EventOptions,
    EventOptionsInput,
    EventsService,
    Hook,
    is_bailed,
)
from .fiber import (
    AsyncEffect,
    CordisError,
    Disposable,
    Effect,
    EffectMeta,
    EffectRunner,
    Fiber,
    FiberState,
    SyncEffect,
    ValidationError,
    resolve_config,
)
from .logger import (
    Exporter,
    Formatter,
    Logger,
    LoggerIntercept,
    LoggerLevel,
    LoggerOptions,
    LoggerService,
    LoggerType,
    Message,
    c16,
    c256,
    default_formatters,
    is_aggregate_error,
)
from .reflect import (
    AccessorGetter,
    AccessorOptions,
    AccessorSetter,
    Impl,
    Property,
    PropertyAccessor,
    ReflectService,
    enhance_error,
    is_special_property,
)
from .registry import (
    Inject,
    InjectSpec,
    PluginBase,
    PluginCallback,
    PluginConfig,
    PluginIntercept,
    PluginLike,
    PluginProvide,
    PluginRuntime,
    RegistryService,
    Runtime,
    StandardSchemaV1,
    resolve_inject,
)
from .service import Service, is_service_instance
from .utils import (
    DisposableList,
    ProtoDict,
    StackInfo,
    Tracker,
    apply_traceable,
    build_outer_stack,
    compose_error,
    create,
    create_callable,
    create_proto_dict,
    create_traceable,
    get_property_descriptor,
    get_proto,
    get_symbol,
    get_traceable,
    has_symbol,
    is_constructor,
    is_nullable,
    is_object,
    join_prototype,
    register_protocol,
    service_symbol,
    set_proto,
    set_symbol,
    symbols,
    with_prop,
    with_props,
)

# camelCase aliases for the JS API
# 为对齐官方 JS API 而保留的 camelCase 别名（与 utils 中的定义一致）
isConstructor = is_constructor
joinPrototype = join_prototype
isObject = is_object
isNullable = is_nullable
getPropertyDescriptor = get_property_descriptor
getTraceable = get_traceable
createTraceable = create_traceable
withProps = with_props
withProp = with_prop
createCallable = create_callable
applyTraceable = apply_traceable
composeError = compose_error
buildOuterStack = build_outer_stack
createProtoDict = create_proto_dict
hasSymbol = has_symbol
setSymbol = set_symbol
getSymbol = get_symbol
setProto = set_proto
getProto = get_proto
resolveConfig = resolve_config
resolveInject = resolve_inject
isBailed = is_bailed
isSpecialProperty = is_special_property
enhanceError = enhance_error
isAggregateError = is_aggregate_error
isServiceInstance = is_service_instance
isContext = is_context

__all__ = [
    # 类型与协议（@dataclass / TypedDict / Protocol 等）
    "AccessorGetter",
    "AccessorOptions",
    "AccessorSetter",
    "AsyncEffect",
    "Context",
    "CordisError",
    "Disposable",
    "DisposableList",
    "Effect",
    "EffectMeta",
    "EffectRunner",
    "EventDispatchError",
    "EventName",
    "EventOptions",
    "EventOptionsInput",
    "EventsService",
    "Exporter",
    "Fiber",
    "FiberState",
    "Formatter",
    "Hook",
    "Impl",
    "Inject",
    "InjectSpec",
    "Logger",
    "LoggerIntercept",
    "LoggerLevel",
    "LoggerOptions",
    "LoggerService",
    "LoggerType",
    "Message",
    "PluginBase",
    "PluginCallback",
    "PluginConfig",
    "PluginIntercept",
    "PluginLike",
    "PluginProvide",
    "PluginRuntime",
    "Property",
    "PropertyAccessor",
    "ProtoDict",
    "ReflectService",
    "RegistryService",
    "Runtime",
    "Service",
    "StackInfo",
    "StandardSchemaV1",
    "SyncEffect",
    "Tracker",
    "ValidationError",
    "applyTraceable",
    "apply_traceable",
    "buildOuterStack",
    "build_outer_stack",
    "c16",
    "c256",
    "composeError",
    "compose_error",
    "create",
    "createCallable",
    "createProtoDict",
    "createTraceable",
    "create_callable",
    "create_proto_dict",
    "create_traceable",
    "default_formatters",
    "enhanceError",
    "enhance_error",
    "getPropertyDescriptor",
    "getProto",
    "getSymbol",
    "getTraceable",
    "get_property_descriptor",
    "get_proto",
    "get_symbol",
    "get_traceable",
    "hasSymbol",
    "has_symbol",
    "isAggregateError",
    "isBailed",
    "isConstructor",
    "isContext",
    "isNullable",
    "isObject",
    "isServiceInstance",
    "isSpecialProperty",
    "is_aggregate_error",
    "is_bailed",
    "is_constructor",
    "is_context",
    "is_nullable",
    "is_object",
    "is_service_instance",
    "is_special_property",
    "joinPrototype",
    "join_prototype",
    "register_protocol",
    "resolveConfig",
    "resolveInject",
    "resolve_config",
    "resolve_inject",
    "service_symbol",
    "setProto",
    "setSymbol",
    "set_proto",
    "set_symbol",
    "symbols",
    "withProp",
    "withProps",
    "with_prop",
    "with_props",
]
