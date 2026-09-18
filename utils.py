"""Infrastructure: ``DisposableList``, ``symbols``, ``Tracker`` and traceables.

Python port of the official ``packages/core/src/utils.ts``.  JavaScript's
``Proxy`` / ``Symbol`` / prototype chain have no direct Python equivalent, so
this module emulates them:

* ``Symbol.for(name)`` -> the singleton :class:`_Symbol` (same name, same object);
* ``obj[symbols.x]`` -> :func:`set_symbol` / :func:`get_symbol`.  Plain objects
  use the ``_cordis_<name>`` attribute slot; objects exposing ``__get_symbol__``
  (such as ``Context``) handle it themselves;
* ``new Proxy(value, handler)`` -> :class:`_TraceableProxy`;
* prototype chain (``Object.create``) -> the ``__proto__`` link read by
  :func:`get_symbol`.

中文说明
--------
本模块是整个 cordis 移植层的「地基」，用 Python 模拟 JavaScript 的若干语言特性：

============================  ==========================================
JavaScript 特性               本模块的模拟方式
============================  ==========================================
``Symbol.for(name)``          :class:`_Symbol` 单例（同名必得同一对象）
``obj[symbols.x]``            :func:`set_symbol` / :func:`get_symbol`
``new Proxy(value, handler)`` :class:`_TraceableProxy` / :class:`_PropsProxy`
``Object.create(proto)``      记录 ``__proto__`` 链接的 :class:`ProtoDict`
``WeakMap`` 引用计数删除        :class:`DisposableList`
============================  ==========================================

其中最重要的两个概念：

* **符号（symbol）槽位**：Python 对象没有「符号键」，因此统一把
  ``obj[symbols.foo]`` 读写为 ``obj._cordis_cordis_foo`` 形式的属性；实现了
  ``__get_symbol__`` / ``__set_symbol__`` 的对象（如 ``Context``、``Service``）
  可以自定义该行为。
* **原型链**：Python 的类继承无法表达「运行期给实例挂原型」，于是用
  ``__proto__`` 属性 + :func:`get_symbol` 的循环上溯来模拟，``isolate`` 与
  ``intercept`` 的分层覆盖正是基于它实现的。
"""

from __future__ import annotations

import inspect
import traceback
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any, NoReturn, TypeVar, cast
from weakref import WeakKeyDictionary

_MISSING = object()
ValueT = TypeVar("ValueT")
ResultT = TypeVar("ResultT")


class DisposableList[T]:
    """Port of TS ``DisposableList<T extends WeakKey>``.

    TS uses ``Map<number, T>`` plus ``WeakMap<T, number>`` for O(1) deletion.
    ``WeakKeyDictionary`` only accepts weak-referenceable keys, so a fallback
    ``id()``-keyed dict keeps a strong reference for the rest.

    Python 说明：
    官方用自增序号做插入顺序、用 ``WeakMap`` 做「值 → 序号」的反查以支持 O(1)
    删除。本实现保留同样的结构：

    * ``_map``：``序号 -> 值``，保证遍历顺序即插入顺序；
    * ``_weak``：``值 -> 序号``，弱引用，可被弱引用的对象走这条路；
    * ``_strong``：不可弱引用的对象（如 ``list`` 的子类）退化为 ``id()`` 强引用。

    ``clear()`` 返回的是「逆序」的值列表——这与官方的卸载顺序一致：后注册的
    先销毁，避免销毁时依赖已被移除的兄弟资源。
    """

    def __init__(self) -> None:
        self._sn: int = 0
        self._map: dict[int, T] = {}
        self._weak: WeakKeyDictionary[T, int] = WeakKeyDictionary()
        self._strong: dict[int, T] = {}

    @property
    def length(self) -> int:
        """元素个数（对应官方 ``list.length``）。"""
        return len(self._map)

    def __len__(self) -> int:
        return self.length

    def _remember(self, value: T, serial: int) -> None:
        """记录「值 → 序号」反查；不可弱引用时退化为强引用字典。"""
        try:
            self._weak[value] = serial
        except TypeError:
            self._strong[id(value)] = value

    def _recall(self, value: T) -> int | None:
        """由值反查序号；找不到返回 ``None``。"""
        try:
            serial = self._weak.get(value)
            return None if serial is None else serial
        except TypeError:
            # 不可弱引用：先确认 id 对应的是同一个对象（防止 id 复用误判），
            # 再线性扫描拿到真正序号
            if self._strong.get(id(value)) is not value:
                return None
            for serial, item in self._map.items():
                if item is value:
                    return serial
            return None

    def push(self, value: T) -> Callable[[], bool]:
        """追加一个元素，返回可幂等的删除函数（重复调用返回 ``False``）。"""
        self._sn += 1
        serial = self._sn
        self._map[serial] = value
        self._remember(value, serial)

        def dispose() -> bool:
            self._strong.pop(id(value), None)
            return self._map.pop(serial, _MISSING) is not _MISSING

        return dispose

    def unshift(self, value: T) -> Callable[[], bool]:
        """Insert at the front, preserving the ordering ``clear()`` relies on.

        The official ``DisposableList`` exposes only ``push``; ``events`` needs a
        prepend for private ``internal/update`` hooks, so it is added here.

        注意：为保证 ``clear()`` 的「后进先出」语义不被破坏，这里采用
        「清空后重放」的方式实现头插，而非直接改内部字典顺序。
        """
        previous = self.clear()
        dispose = self.push(value)
        for item in previous:
            self.push(item)
        return dispose

    def delete(self, value: T) -> bool:
        """按值删除；成功返回 ``True``。"""
        serial = self._recall(value)
        if serial is None:
            return False
        self._strong.pop(id(value), None)
        return self._map.pop(serial, _MISSING) is not _MISSING

    def clear(self) -> list[T]:
        """清空并逆序返回所有元素（供卸载时按「后进先出」顺序处理）。"""
        values = list(self._map.values())
        self._map.clear()
        self._weak = WeakKeyDictionary()
        self._strong.clear()
        values.reverse()
        return values

    def __iter__(self) -> Iterator[T]:
        return iter(self._map.values())

    def __repr__(self) -> str:
        return repr(list(self))


class _Symbol:
    """Equivalent of ``Symbol.for(name)``: same name always yields same object.

    与 JS 的 ``Symbol.for`` 语义一致：全局注册表保证同名符号是同一对象，
    可以用 ``is`` 比较，也可安全地作为字典键。
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"Symbol.for({self.name!r})"

    def __str__(self) -> str:
        return self.name


# 全局符号注册表：名字 -> 单例符号
_symbol_registry: dict[str, _Symbol] = {}


def _symbol_for(name: str) -> _Symbol:
    """对应 ``Symbol.for(name)``：同名复用同一对象。"""
    symbol = _symbol_registry.get(name)
    if symbol is None:
        symbol = _Symbol(name)
        _symbol_registry[name] = symbol
    return symbol


def _new_symbol(name: str) -> _Symbol:
    """Create a fresh symbol, equivalent to JavaScript ``Symbol(name)``.

    对应 JS 的 ``Symbol(name)``（非注册表版本）：每次调用都生成新对象，
    因此在 ``interface`` 比较中天然唯一——这正是服务隔离所需的性质。
    """
    return _Symbol(name)


_SERVICE_PREFIX = "cordis.service."


def service_symbol(name: str) -> _Symbol:
    """Symbol key created by ``ReflectService.provide`` (``Symbol(name)``).

    为服务名生成唯一键；前缀让调试时一眼看出它来自 ``provide``。
    """
    return _new_symbol(_SERVICE_PREFIX + name)


class _Symbols:
    """Mirrors the official ``symbols`` object one-to-one.

    一一对应官方 ``symbols`` 常量表。按用途分为四组：内部机制、上下文协议、
    服务协议、以及 ``Context.is`` 类型协议。
    """

    # internal symbols —— 内部机制
    shadow = _symbol_for("cordis.shadow")          # 定义点（def site）上下文
    caller = _symbol_for("cordis.caller")          # 调用方上下文
    receiver = _symbol_for("cordis.receiver")      # 属性访问的接收者
    original = _symbol_for("cordis.original")      # 追踪代理背后的原始对象
    metadata = _symbol_for("cordis.metadata")      # 装饰器元数据（如 @Inject）
    initHooks = _symbol_for("cordis.initHooks")    # 实例初始化钩子列表
    checkProto = _symbol_for("cordis.checkProto")  # 标记 inject 已走原型合并

    # context symbols —— 上下文协议
    effect = _symbol_for("cordis.effect")          # 副作用元信息（EffectMeta）
    filter = _symbol_for("cordis.filter")          # 事件过滤协议
    isolate = _symbol_for("cordis.isolate")        # 隔离层（服务可见性边界）
    intercept = _symbol_for("cordis.intercept")    # 拦截层（配置覆盖）

    # service symbols —— 服务协议
    init = _symbol_for("cordis.init")                      # 服务初始化
    check = _symbol_for("cordis.check")                    # 服务可用性检查
    config = _symbol_for("cordis.config")                  # 服务配置
    invoke = _symbol_for("cordis.invoke")                  # 可调用服务入口
    extend = _symbol_for("cordis.extend")                  # 服务视图派生
    tracker = _symbol_for("cordis.tracker")                # 追踪器
    resolveConfig = _symbol_for("cordis.resolveConfig")    # 配置合并

    # `Context.is` protocol symbol (`Symbol.for('cordis.is')`)
    is_context = _symbol_for("cordis.is")  # 类型判断协议（``Context.is``）


#: 全局符号表，使用处形如 ``symbols.tracker``
symbols = _Symbols()


@dataclass
class Tracker:
    """Port of TS ``interface Tracker``.

    追踪器：描述「如何从一个对象追踪到它所属的上下文/服务」。

    字段:
        associate: 关联的服务名。访问 ``ctx['服务名.成员']`` 时，若命中该服务的
            ``props``，会带 ``receiver`` 转发到该服务（见 ``_TraceableProxy``）。
        property: 该对象持有上下文时所用的属性名（通常是 ``ctx`` 或 ``fiber``）。
        no_shadow: 为 ``True`` 时不创建 shadow 上下文（用于无需区分定义点的对象，
            如 ``Context`` 自身、事件服务）。
    """

    associate: str | None = None
    property: str | None = None
    no_shadow: bool = False


_SYMBOL_PREFIX = "_cordis_"
_PROTO_ATTR = "__proto__"

# Protocol symbols that map to Python dunder methods, so subclasses can simply
# define e.g. ``__invoke__`` / ``__filter__`` and still satisfy the protocol.
# 协议符号 -> dunder 方法名 的映射表（由 ``Service`` 等模块注册）。
_PROTOCOL_METHODS: dict[_Symbol, str] = {}


def register_protocol(symbol: _Symbol, attr: str) -> _Symbol:
    """Declare that ``symbol`` is served by the ``attr`` dunder method.

    声明「符号 ``symbol`` 由名为 ``attr`` 的 dunder 方法实现」。注册后，
    ``get_symbol`` 在实例上找不到符号槽位时会回退到该方法。
    """
    _PROTOCOL_METHODS[symbol] = attr
    return symbol


def _symbol_attr(symbol: _Symbol) -> str:
    """符号对应的属性名：``cordis.tracker`` -> ``_cordis_cordis_tracker``。"""
    return _SYMBOL_PREFIX + symbol.name.replace(".", "_")


def protocol_attr(symbol: _Symbol) -> str | None:
    """查询符号注册的 dunder 方法名；未注册返回 ``None``。"""
    return _PROTOCOL_METHODS.get(symbol)


def set_symbol(value: Any, symbol: _Symbol, data: Any) -> Any:
    """Port of ``obj[symbols.x] = data``.

    写入符号槽位，优先级：
    1. 对象自定义的 ``__set_symbol__``（如 ``Context`` / ``Service``）；
    2. ``dict`` 直接用符号作键；
    3. 已存在的符号属性直接覆盖；
    4. 否则用 ``object.__setattr__`` 新建，失败则落到 ``__dict__``。
    """
    custom = getattr(type(value), "__set_symbol__", None)
    if custom is not None:
        custom(value, symbol, data)
        return value
    if isinstance(value, dict):
        value[symbol] = data
        return value
    state = getattr(value, "__dict__", None)
    attr = _symbol_attr(symbol)
    if state is not None and attr in state:
        state[attr] = data
        return value
    try:
        object.__setattr__(value, attr, data)
    except (AttributeError, TypeError):
        # 内置类型（如某些 C 实现对象）无法挂属性时，退回到 __dict__
        if state is not None:
            state[attr] = data
    return value


def get_symbol(value: Any, symbol: _Symbol, default: Any = None) -> Any:
    """Read a symbol slot, walking the ``__proto__`` chain (prototype inherit).

    读取符号槽位，查找顺序：
    1. 对象自定义的 ``__get_symbol__``；
    2. 沿 ``__proto__`` 原型链逐层向上查（含类属性表 ``vars(type(...))``）；
    3. 协议 dunder 回退（见 :func:`register_protocol`）；
    4. ``default``。
    """
    custom = getattr(type(value), "__get_symbol__", None)
    if custom is not None:
        return custom(value, symbol, default)
    current = value
    seen: set[int] = set()  # 防御循环原型链，避免死循环
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, dict):
            if symbol in current:
                return current[symbol]
        else:
            state = getattr(current, "__dict__", None)
            attr = _symbol_attr(symbol)
            if state is not None and attr in state:
                return state[attr]
            if attr in vars(type(current)):
                return vars(type(current))[attr]
        current = get_proto(current)
    # 实例与原型链都没有实体槽位时，尝试协议 dunder（例如 __invoke__）
    protocol = protocol_attr(symbol)
    if protocol is not None:
        method = getattr(value, protocol, None)
        if callable(method):
            return method
    return default


def has_symbol(value: Any, symbol: _Symbol) -> bool:
    """符号槽位是否存在（用 ``_MISSING`` 哨兵区分「值为 None」与「不存在」）。"""
    return get_symbol(value, symbol, _MISSING) is not _MISSING


def get_proto(value: Any) -> Any:
    """读取 ``__proto__`` 原型链接（dict 与普通对象两种存放方式）。"""
    state = getattr(value, "__dict__", None)
    if state is not None and _PROTO_ATTR in state:
        return state[_PROTO_ATTR]
    if isinstance(value, dict):
        return value.get(_PROTO_ATTR)
    return None


def set_proto(value: Any, prototype: Any) -> Any:
    """Port of ``Object.create(prototype)`` prototype link.

    建立「实例 → 原型」链接：``Context.extend`` 用它把子上下文挂到父上下文上，
    从而使符号查找与 ``isolate`` 覆盖具有继承语义。
    """
    if isinstance(value, dict):
        value[_PROTO_ATTR] = prototype
        return value
    state = getattr(value, "__dict__", None)
    if state is not None:
        state[_PROTO_ATTR] = prototype
    return value


def create(prototype: Any = None) -> dict[str, Any]:
    """``Object.create(proto)`` -> a dict carrying a ``__proto__`` link."""
    return {_PROTO_ATTR: prototype}


def is_constructor(func: Any) -> bool:
    """Port of TS ``isConstructor``.

    The official code rejects arrow functions (no ``prototype``) and generator /
    async-generator functions.  In Python only classes are constructors.

    判定「插件是否以类/构造函数形式提供」：为真时会先实例化插件再调用其
    ``init`` 方法（见 ``Fiber._run_plugin``）。
    """
    return inspect.isclass(func)


def is_object(value: Any) -> bool:
    """Port of TS ``isObject``: objects and functions, not ``None``/scalars.

    JS 中数组、函数、普通对象都算 object；这里排除 ``None``、布尔与数字、
    字符串等标量，其余（含类实例、函数、列表、字典）均视为对象。
    """
    if value is None or value is True or value is False:
        return False
    return not isinstance(value, (str, bytes, int, float, complex))


class ProtoDict(dict[Any, Any]):
    """Dict with a prototype link, emulating ``Object.create(parent)``.

    Reads fall back to the prototype, writes stay local.  This is what makes
    ``isolate``/``intercept`` layers behave like the official prototype chain:
    later writes to the root layer remain visible to children.

    读操作沿原型链回退、写操作只落在当前层——这正是 ``isolate``（隔离）与
    ``intercept``（拦截）可实现「层叠覆盖」的关键：子层可以覆盖单项，而未覆盖的
    项仍然读到底层之后的新值。
    """

    def __init__(
        self,
        mapping: dict[Any, Any] | None = None,
        proto: ProtoDict | None = None,
    ) -> None:
        # Keep the convenient ``ProtoDict({...})`` form while allowing
        # ``ProtoDict(proto=parent)`` to model ``Object.create(parent)``.
        # 兼容两种写法：ProtoDict({...}) 与 ProtoDict(proto=parent)
        if isinstance(mapping, ProtoDict) and proto is None:
            proto = mapping
            mapping = None
        super().__init__(mapping or {})
        self.proto = proto

    # reads walk the chain —— 以下读取操作都会沿链回退
    def get(self, key: Any, default: Any = None) -> Any:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        if self.proto is not None:
            return self.proto.get(key, default)
        return default

    def __contains__(self, key: object) -> bool:
        if dict.__contains__(self, key):
            return True
        return self.proto is not None and key in self.proto

    def __getitem__(self, key: Any) -> Any:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        if self.proto is not None:
            return self.proto[key]
        raise KeyError(key)

    def has_own(self, key: Any) -> bool:
        """``Object.hasOwn``: only the local layer.

        仅判断当前层（不沿链），对应 JS 的 ``Object.hasOwn``。
        """
        return dict.__contains__(self, key)

    def own_keys(self) -> list[Any]:
        """当前层的键（不含原型链）。"""
        return list(dict.keys(self))

    def own_items(self) -> list[tuple[Any, Any]]:
        """当前层的键值对（不含原型链）。"""
        return list(dict.items(self))

    def chain(self) -> Iterator[ProtoDict]:
        """Yield this layer and every prototype layer.

        从当前层向上依次产出每一层，供合并配置/拦截时确定优先级。
        """
        current: ProtoDict | None = self
        while current is not None:
            yield current
            current = current.proto

    def to_dict(self) -> dict[Any, Any]:
        """Flatten the whole chain (prototypes first).

        把整条链拍平成一个普通字典；先放入最底层，因此上层覆盖下层。
        """
        result: dict[Any, Any] = {}
        for layer in reversed(list(self.chain())):
            result.update(dict.items(layer))
        return result

    def __repr__(self) -> str:
        return f"ProtoDict({dict(self)!r}, proto={self.proto!r})"


def create_proto_dict(mapping: dict[Any, Any] | None = None) -> ProtoDict:
    """``createProtoDict`` 工厂函数（camelCase 别名见文件末尾）。"""
    return ProtoDict(mapping=mapping)


def is_nullable(value: Any) -> bool:
    """Port of cosmokit ``isNullable``: only the Python null value.

    与 JS ``value == null``（null/undefined）对应：Python 只有 ``None``。
    """
    return value is None


def get_property_descriptor(target: Any, prop: str) -> Any:
    """Value of ``Reflect.getOwnPropertyDescriptor(...).value`` along the MRO.

    模拟 ``Reflect.getOwnPropertyDescriptor``：按「实例字典 → 类 MRO → 原型链 →
    ``getattr`` 兜底」的顺序取值；``property`` 返回其 getter 函数，``staticmethod``
    返回底层函数（保持未绑定，交由调用方决定是否传 receiver）。
    """
    if isinstance(target, dict):
        return target.get(prop)
    state = getattr(target, "__dict__", None)
    if state is not None and prop in state:
        return state[prop]
    for cls in type(target).__mro__:
        descriptor = vars(cls).get(prop)
        if descriptor is not None:
            return _descriptor_value(descriptor)
    proto = get_proto(target)
    if proto is not None:
        return get_property_descriptor(proto, prop)
    try:
        return getattr(target, prop)
    except Exception:  # noqa: BLE001
        return None


def _descriptor_value(descriptor: Any) -> Any:
    """把描述符还原为「可用值」：property → fget，静态/类方法 → 原函数。"""
    if isinstance(descriptor, property):
        return descriptor.fget
    if isinstance(descriptor, (staticmethod, classmethod)):
        return descriptor.__func__
    return descriptor


def join_prototype(proto1: Any, proto2: Any) -> Any:
    """Port of TS ``joinPrototype``: chain ``proto1``'s prototype under ``proto2``.

    把两条原型合并：``proto1`` 已有的成员优先，其余从 ``proto2`` 继承。
    Python 中通过动态创建子类 ``(proto1, proto2)`` 近似实现（MRO 保证优先级）。
    """
    if proto1 is None or proto1 is object:
        return proto2
    if not inspect.isclass(proto1) or not inspect.isclass(proto2):
        return proto2
    if proto1 is proto2 or issubclass(proto1, proto2):
        return proto1
    try:
        return type("JoinedPrototype", (proto1, proto2), {"__module__": __name__})
    except TypeError:
        # MRO 冲突（布局不兼容）时放弃合并，保留 proto1
        return proto1


# ---------------------------------------------------------------------------
# traceable —— 可追踪代理
# ---------------------------------------------------------------------------


class _TraceableProxy:
    """Port of the ``Proxy`` returned by ``createTraceable``.

    Python cannot intercept meta operations, so ``proxy.original`` yields the raw
    object, ``proxy.caller`` the def site, ``proxy.receiver`` the receiver, and
    ``tracker.property`` the use site.

    追踪代理：包装一个「携带 tracker 的值」，在读取成员时补齐上下文信息，
    官方通过 ``Proxy`` 元操作实现；Python 只能逐成员拦截，因此约定：

    * ``proxy.original`` → 原始对象（脱掉代理）；
    * ``proxy.caller``   → 定义点（def site）上下文；
    * ``proxy.receiver`` → 当前代理自身（服务方法第一个参数）；
    * ``proxy.<tracker.property>``（通常是 ``ctx``）→ 使用点（use site）上下文。
    """

    def __init__(self, ctx: Any, value: Any, tracker: Tracker) -> None:
        # 用 object.__setattr__ 绕过本类的 __setattr__，避免自我递归
        object.__setattr__(self, "_trace_value", value)
        object.__setattr__(self, "_trace_tracker", tracker)
        object.__setattr__(self, "_trace_ctx", ctx)

    @property
    def _def_site(self) -> Any:
        """定义点：优先取 ``symbols.shadow``（明确标记的定义上下文）。"""
        shadow = get_symbol(self, symbols.shadow)
        if shadow is not None:
            return shadow
        return object.__getattribute__(self, "_trace_ctx")

    @property
    def _use_site(self) -> Any:
        """使用点：调用方上下文，用于事件过滤与依赖归属判定。"""
        ctx = object.__getattribute__(self, "_trace_ctx")
        if isinstance(ctx, _TraceableProxy):
            return object.__getattribute__(ctx, "_trace_ctx")
        shadow = get_symbol(ctx, symbols.shadow)
        if shadow is not None:
            # shadow 上下文本身只是「定义点标记」，真正的使用点是它的原型（父上下文）
            prototype = get_proto(ctx)
            return prototype if prototype is not None else ctx
        return ctx

    def _shadow_for(self, target: Any) -> Any:
        """为 ``target`` 构造一个带 ``symbols.shadow`` 标记的子上下文。

        作用：把「调用发生在哪个上下文」固定下来，后续通过 ``tracker.property``
        暴露给方法调用者（例如服务方法的第一个 ``ctx`` 参数）。
        """
        tracker = object.__getattribute__(self, "_trace_tracker")
        if not tracker.property:
            return self
        value = get_property_descriptor(target, tracker.property)
        if value is None:
            return self
        def_site = get_symbol(value, symbols.shadow) or value
        ctx = object.__getattribute__(self, "_trace_ctx")
        shadow = ctx.extend({symbols.shadow: def_site})
        return with_prop(self, tracker.property, shadow)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_trace_"):
            raise AttributeError(name)
        tracker = object.__getattribute__(self, "_trace_tracker")
        target = object.__getattribute__(self, "_trace_value")
        # 四个内建视图：original / caller / receiver / tracker.property
        if name == "original":
            return target
        if name == "caller":
            return self._def_site
        if name == "receiver":
            return self
        if name == tracker.property:
            return self._use_site

        use_site = self._use_site
        associate = tracker.associate
        if associate:
            # 形如 ``ctx['服务名.成员']``：转发到对应服务的已声明属性
            props = getattr(getattr(use_site, "reflect", None), "props", None) or {}
            dotted = f"{associate}.{name}"
            if dotted in props:
                receiver = with_prop(self, symbols.receiver, self)
                return use_site.get(dotted, receiver=receiver)

        descriptor = _find_class_descriptor(target, name)
        if inspect.isfunction(descriptor) and not tracker.no_shadow:
            # 普通方法：包一层 shadow 方法，保证调用时能拿到正确的定义点
            shadow = self._shadow_for(target)
            return create_shadow_method(use_site, descriptor, self, shadow, shadow)
        if isinstance(descriptor, staticmethod):
            return create_shadow_method(
                use_site,
                descriptor.__func__,
                self,
                self,
                None,
            )

        # JS `Reflect.get` yields members already bound to the target; Python's
        # descriptors return unbound functions, so instance access is preferred
        # and the raw descriptor is only a fallback.
        try:
            inner_value = getattr(target, name)
        except AttributeError:
            inner_value = get_property_descriptor(target, name)
            if inner_value is None:
                raise
        # 成员自身带 tracker 时，递归包装（如 ctx → ctx.logger → logger 实例）
        inner_tracker = get_symbol(inner_value, symbols.tracker) if inner_value is not None else None
        if inner_tracker is not None:
            return create_traceable(use_site, inner_value, inner_tracker)
        if not tracker.no_shadow and callable(inner_value):
            shadow = self._shadow_for(target)
            return create_shadow_method(use_site, inner_value, self, shadow)
        return inner_value

    def __get_symbol__(self, symbol: _Symbol, default: Any = None) -> Any:
        """符号读取：四个特殊符号映射到代理视图，其余透传给原始对象。"""
        target = object.__getattribute__(self, "_trace_value")
        if symbol == symbols.original:
            return target
        if symbol == symbols.caller:
            return self._def_site
        if symbol == symbols.receiver:
            return self
        if symbol == symbols.tracker:
            return object.__getattribute__(self, "_trace_tracker")
        return get_symbol(target, symbol, default)

    def __set_symbol__(self, symbol: _Symbol, data: Any) -> None:
        """符号写入：original/caller/receiver 为只读视图，其余写到原始对象上。"""
        if symbol in (symbols.original, symbols.caller, symbols.receiver):
            return
        set_symbol(object.__getattribute__(self, "_trace_value"), symbol, data)

    def __has_symbol__(self, symbol: _Symbol) -> bool:
        return self.__get_symbol__(symbol, _MISSING) is not _MISSING

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_trace_"):
            object.__setattr__(self, name, value)
            return
        tracker = object.__getattribute__(self, "_trace_tracker")
        target = object.__getattribute__(self, "_trace_value")
        if name in ("original", "caller", tracker.property):
            raise AttributeError(f'cannot set property "{name}"')
        use_site = self._use_site
        associate = tracker.associate
        if associate:
            # 服务属性写入：走 ``ctx.set('服务名.成员', value)`` 的统一入口
            props = getattr(getattr(use_site, "reflect", None), "props", None) or {}
            dotted = f"{associate}.{name}"
            if dotted in props:
                receiver = with_prop(self, symbols.receiver, self)
                use_site.set(dotted, value, receiver=receiver)
                return
        setattr(target, name, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """调用代理：按 tracker 决定 receiver，再交给 ``apply_traceable``。"""
        target = object.__getattribute__(self, "_trace_value")
        tracker = object.__getattribute__(self, "_trace_tracker")
        receiver = self if tracker.no_shadow else self._shadow_for(target)
        return apply_traceable(receiver, target, self, args, kwargs)

    def __repr__(self) -> str:
        return repr(object.__getattribute__(self, "_trace_value"))

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        # 与原始对象视为相等，便于 ``ctx.foo is service`` 之类的判断
        return object.__getattribute__(self, "_trace_value") is other

    def __hash__(self) -> int:
        return id(object.__getattribute__(self, "_trace_value"))


def _find_class_descriptor(target: Any, name: str) -> Any:
    """沿 MRO 查找类属性描述符（不触发 ``__getattr__``）。"""
    for cls in type(target).__mro__:
        if name in vars(cls):
            return vars(cls)[name]
    return None


def create_shadow_method(
    ctx: Any,
    value: Any,
    outer: Any,
    shadow: Any,
    receiver: Any = _MISSING,
) -> Callable[..., Any]:
    """Port of TS ``createShadowMethod``.

    The official version swaps ``thisArg === outer`` with ``shadow`` via
    ``Proxy.apply``.  Python has no dynamic ``this``, so this returns a wrapper
    that forwards the call and traces the result.

    参数:
        ctx: 结果的追踪上下文（使用点）。
        value: 原始函数。
        outer: 代理自身（Python 版本未直接使用，保留以对齐签名）。
        shadow: 定义点上下文（保留以对齐签名）。
        receiver: 若给出则作为 ``self`` 显式传入（服务方法场景）。
    """

    def shadow_method(*args: Any, **kwargs: Any) -> Any:
        if receiver is _MISSING or receiver is None:
            result = value(*args, **kwargs)
        else:
            result = value(receiver, *args, **kwargs)
        return get_traceable(ctx, result)

    return shadow_method


def get_traceable(ctx: Any, value: ValueT) -> ValueT:
    """把一个值按需包装成追踪代理；标量与无 tracker 的值原样返回。"""
    if not is_object(value):
        return value
    if isinstance(value, _TraceableProxy):
        return cast(ValueT, value)
    if _has_own_symbol(value, symbols.shadow):
        # 已是 shadow 标记对象：返回其原型（即「真正的对象」）
        prototype = get_proto(value)
        return cast(ValueT, prototype if prototype is not None else value)
    tracker = get_symbol(value, symbols.tracker)
    if tracker is None:
        return value
    return create_traceable(ctx, value, tracker)


def _has_own_symbol(value: Any, symbol: _Symbol) -> bool:
    """判断符号是否为「自有」（不沿原型链），对应 ``Object.hasOwn``。"""
    if isinstance(value, dict):
        return symbol in value
    state = getattr(value, "__dict__", None)
    if state is not None:
        # Context 把符号存在 ``_symbols`` 字典里，需单独检查
        local_symbols = state.get("_symbols")
        if isinstance(local_symbols, dict) and symbol in local_symbols:
            return True
        if symbol in state:
            return True
        if _symbol_attr(symbol) in state:
            return True
    return _symbol_attr(symbol) in vars(type(value))


def create_traceable(ctx: Any, value: ValueT, tracker: Tracker) -> ValueT:
    """创建追踪代理（对应官方 ``createTraceable``）。"""
    return cast(ValueT, _TraceableProxy(ctx, value, tracker))


class _PropsProxy:
    """Port of the ``withProps`` Proxy: redirect reads/writes for ``props`` keys.

    属性覆盖代理：``props`` 中列出的键读写都落在临时映射上，其余透传给目标对象。
    典型用途是把 ``symbols.receiver`` / ``symbols.shadow`` 之类「本次调用的上下文」
    附加到对象上，而不污染对象本身。
    """

    def __init__(self, target: Any, props: dict[Any, Any]) -> None:
        object.__setattr__(self, "_props_target", target)
        object.__setattr__(self, "_props_map", props)

    @property
    def __class__(self) -> type[Any]:
        """Expose the target class to Python's proxy-aware isinstance check."""
        return type(object.__getattribute__(self, "_props_target"))

    @property
    def __dict__(self) -> dict[str, Any]:
        """Expose the target instance dictionary instead of proxy internals."""
        target = object.__getattribute__(self, "_props_target")
        state = getattr(target, "__dict__", None)
        return state if isinstance(state, dict) else {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_props_"):
            raise AttributeError(name)
        props = object.__getattribute__(self, "_props_map")
        if name != "constructor" and name in props:
            return props[name]
        return getattr(object.__getattribute__(self, "_props_target"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_props_"):
            object.__setattr__(self, name, value)
            return
        props = object.__getattribute__(self, "_props_map")
        if name != "constructor" and name in props:
            props[name] = value
            return
        setattr(object.__getattribute__(self, "_props_target"), name, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return object.__getattribute__(self, "_props_target")(*args, **kwargs)

    def __getitem__(self, name: Any) -> Any:
        props = object.__getattribute__(self, "_props_map")
        if name in props:
            return props[name]
        return object.__getattribute__(self, "_props_target")[name]

    def __setitem__(self, name: Any, value: Any) -> None:
        props = object.__getattribute__(self, "_props_map")
        if name in props:
            props[name] = value
            return
        object.__getattribute__(self, "_props_target")[name] = value

    def __get_symbol__(self, symbol: _Symbol, default: Any = None) -> Any:
        props = object.__getattribute__(self, "_props_map")
        if symbol in props:
            return props[symbol]
        return get_symbol(object.__getattribute__(self, "_props_target"), symbol, default)

    def __set_symbol__(self, symbol: _Symbol, data: Any) -> None:
        props = object.__getattribute__(self, "_props_map")
        if symbol in props:
            props[symbol] = data
            return
        set_symbol(object.__getattribute__(self, "_props_target"), symbol, data)

    def __has_symbol__(self, symbol: _Symbol) -> bool:
        props = object.__getattribute__(self, "_props_map")
        if symbol in props:
            return True
        return has_symbol(object.__getattribute__(self, "_props_target"), symbol)

    def __repr__(self) -> str:
        return repr(object.__getattribute__(self, "_props_target"))


def with_props(target: Any, props: dict[Any, Any] | None = None) -> Any:
    """``withProps``：附加临时属性；``props`` 为空时直接返回原对象。"""
    if not props:
        return target
    return _PropsProxy(target, props)


def with_prop(target: Any, prop: Any, value: Any) -> Any:
    """``withProps`` 的单属性简写。"""
    return with_props(target, {prop: value})


def create_callable(name: str, proto: Any, tracker: Tracker) -> Any:
    """Port of TS ``createCallable``: build a callable object carrying a tracker.

    ``proto`` behaves like ``Object.setPrototypeOf``: classes become bases so the
    instance passes ``isinstance`` checks; dicts contribute members directly.

    参数:
        name: 可调用对象的 ``__name__``（也用作动态类名）。
        proto: 原型来源：类 → 作为基类（保留 isinstance 语义）；
            其他对象 → 继承其类型并复制实例字典；字典 → 直接补充成员。
        tracker: 附加到新对象上的追踪器。
    """

    bases: tuple[type, ...] = ()
    if inspect.isclass(proto) and proto is not object:
        bases = (proto,)

    namespace: dict[str, Any] = {"__module__": __name__}

    def __call__(self: Any, *args: Any, **kwargs: Any) -> Any:
        # 调用时以 caller（调用方上下文）为使用点创建追踪代理
        ctx = get_symbol(self, symbols.caller) or getattr(self, "ctx", None)
        proxy = create_traceable(ctx, self, tracker)
        return apply_traceable(proxy, self, None, args, kwargs)

    namespace["__call__"] = __call__
    namespace["__name__"] = name
    if isinstance(proto, dict):
        namespace.update(proto)

    # 动态类名必须是合法标识符，非法字符统一替换为下划线
    safe_name = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name) or "Callable"
    if not bases and proto is not None and not inspect.isclass(proto):
        bases = (type(proto),)
    callable_type = type(safe_name, bases or (object,), namespace)
    instance = object.__new__(callable_type)
    if not inspect.isclass(proto) and getattr(proto, "__dict__", None):
        instance.__dict__.update(proto.__dict__)
    set_symbol(instance, symbols.tracker, tracker)
    return instance


def apply_traceable(
    proxy: Any,
    value: Any,
    this_arg: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any] | None = None,
) -> Any:
    """Port of TS ``applyTraceable``: prefer the ``symbols.invoke`` protocol.

    调用一个「可追踪值」，按优先级分派：
    1. 类上定义的 ``__invoke__``（服务/工具类）；
    2. 符号槽位 ``symbols.invoke``（且不是自身，避免递归）；
    3. 直接调用原值。
    """
    invoke = get_symbol(value, symbols.invoke)
    raw_invoke = getattr(type(value), "__invoke__", None)
    if callable(raw_invoke):
        # 传入既有代理作为 self，使 ``__invoke__`` 能读到 caller 上下文
        return raw_invoke(proxy, *args, **(kwargs or {}))
    if invoke is not None and invoke is not value:
        return invoke(*args, **(kwargs or {}))
    if kwargs:
        return value(*args, **kwargs)
    return value(*args)


class StackInfo:
    """Port of TS ``interface StackInfo``.

    长栈（long stack）辅助信息：``offset`` 用于裁剪无关栈帧，``error`` 是占位
    异常（生成栈快照用，不代表真实错误）。
    """

    __slots__ = ("error", "offset")

    def __init__(self, offset: int, error: BaseException) -> None:
        self.offset = offset
        self.error = error


def _handle_error(
    info: StackInfo,
    reason: BaseException,
    get_outer_stack: Callable[[], list[str]],
) -> NoReturn:
    """Port of TS ``handleError``: append the outer stack (Python ``__notes__``).

    把「外层栈快照」作为 note 追加到异常上（Python 3.11+ 的 ``__notes__`` 会在
    traceback 中显示），从而近似 JS 的长栈错误链。
    """
    outer = get_outer_stack()
    if outer:
        note = "Outer stack:\n" + "\n".join(line.rstrip("\n") for line in outer)
        notes = getattr(reason, "__notes__", None)
        if notes is None:
            notes = []
            try:
                reason.__notes__ = notes  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                notes = None
        if notes is not None:
            notes.append(note)
    raise reason


def compose_error(
    callback: Callable[[StackInfo], ResultT],
    get_outer_stack: Callable[[], list[str]] | None = None,
) -> ResultT:
    """Port of TS ``composeError``: long-stack enhancement for sync and async.

    执行 ``callback`` 并统一做长栈增强：同步异常立即附加外层栈；返回可等待对象时
    包装成协程，在 ``await`` 抛出异常时同样附加外层栈。
    """
    if get_outer_stack is None:
        get_outer_stack = build_outer_stack()
    info = StackInfo(1, RuntimeError())
    try:
        result = callback(info)
    except BaseException as reason:
        _handle_error(info, reason, get_outer_stack)
        raise
    if is_object(result) and hasattr(result, "__await__"):
        async_result = cast(Awaitable[Any], result)

        async def _await() -> Any:
            try:
                return await async_result
            except BaseException as reason:
                _handle_error(info, reason, get_outer_stack)
                raise
        return cast(ResultT, _await())
    return result


def build_outer_stack(offset: int = 0) -> Callable[[], list[str]]:
    """Port of TS ``buildOuterStack``: snapshot the stack for later splicing.

    在创建时抓取当前调用栈快照，返回一个「取快照」的闭包；后续出错时调用闭包即可
    把外层栈信息附加到异常上（跨异步边界仍然有效）。
    """
    stack = traceback.format_stack()

    def get_outer_stack() -> list[str]:
        # 跳过 build_outer_stack 自身及调用方的前几帧，避免噪音
        return stack[3 + offset:] if len(stack) > 3 + offset else []

    return get_outer_stack


# camelCase aliases, matching the official export names
# 与官方导出名一致的 camelCase 别名，便于对照 TS 文档/源码使用
isConstructor = is_constructor
joinPrototype = join_prototype
isObject = is_object
isNullable = is_nullable
getPropertyDescriptor = get_property_descriptor
getTraceable = get_traceable
createTraceable = create_traceable
withProps = with_props
withProp = with_prop
createShadowMethod = create_shadow_method
createCallable = create_callable
applyTraceable = apply_traceable
composeError = compose_error
buildOuterStack = build_outer_stack
hasSymbol = has_symbol
setSymbol = set_symbol
getSymbol = get_symbol
setProto = set_proto
getProto = get_proto