import builtins
import math
import operator
import sys
from dataclasses import dataclass, field
from enum import auto, Enum
from functools import partial, reduce
from numbers import Number
from typing import Dict, Sequence, Tuple, Union

import thunder.core.dtypes as dtypes
import thunder.core.utils as utils

from .proxies import NumberProxy, Proxy, proxy, TensorProxy
from .trace import get_trace
from .utils import check, get_numberlike_value, same_shape

# This file defines Thunder's "primitive" operations. These are the
#   "building blocks" for all of Thunder's operators.

# Transforms and analysis defined on the primitive operations should
#   be inherited by the operation's they're composed of.

# This file depends on trace.py, the dtypes submodule, proxies.py, and utils.py.

__all__ = [
    # Methods and datastructures for constructing primitive operations
    "make_prim",
    # Data movement and transformation prims
    "convert_element_type",
    # Tensor creation prims
    "full",
    "iota",
    # TODO: review randomness prims
    "uniform",
    # Shape prims
    "broadcast_in_dim_meta",
    "broadcast_in_dim",
    "reshape",
    "slice",
    "squeeze",
    "transpose",
    # NOTE: view is EXPERIMENTAL ONLY
    "view",
    # Elementwise unary prims
    "abs",
    "acos",
    "acosh",
    "asin",
    "atan",
    "atanh",
    "bitwise_not",
    "ceil",
    "cos",
    "cosh",
    "erf",
    "erfc",
    "exp",
    "expm1",
    "floor",
    "isfinite",
    "rsqrt",
    "sin",
    "tanh",
    "log",
    "log10",
    "log1P",
    "log2",
    # Elementwise binary prims
    "add",
    "atan2",
    "bitwise_and",
    "div",
    "eq",
    "lt",
    "mul",
    "pow",
    "sub",
    # Elementwise ternary prims
    "where",
    # Reduction prims
    "reduction_meta",
    "sum_meta",
    "amax",
    "sum",
    "var",
    "var_meta",
    # Matmul prims
    "linear",
    "matmul",
    # NN prims
    "embedding",
]


class Ops(Enum):
    # Data movement and transformation prims
    CONVERT_ELEMENT_TYPE = auto()
    # Tensor creation prims
    FULL = auto()
    IOTA = auto()
    UNIFORM = auto()
    # Shape prims
    BROADCAST_IN_DIM = auto()
    RESHAPE = auto()
    SLICE = auto()
    SQUEEZE = auto()
    TRANSPOSE = auto()
    VIEW = auto()
    # Elementwise unary prims
    ABS = auto()
    ACOS = auto()
    ACOSH = auto()
    ASIN = auto()
    ATAN = auto()
    ATANH = auto()
    BITWISE_NOT = auto()
    CEIL = auto()
    COS = auto()
    COSH = auto()
    ERF = auto()
    ERFC = auto()
    EXP = auto()
    EXPM1 = auto()
    FLOOR = auto()
    ISFINITE = auto()
    RSQRT = auto()
    SIN = auto()
    TANH = auto()
    LOG = auto()
    LOG10 = auto()
    LOG1P = auto()
    LOG2 = auto()
    # Elementwise binary prims
    ADD = auto()
    ATAN2 = auto()
    BITWISE_AND = auto()
    DIV = auto()
    EQ = auto()
    LT = auto()
    MUL = auto()
    POW = auto()
    SUB = auto()
    # Elementwise ternary prims
    WHERE = auto()
    # Reduction prims
    AMAX = auto()
    SUM = auto()
    VAR = auto()
    # Matmul prims
    LINEAR = auto()
    MATMUL = auto()
    # NN prims
    EMBEDDING = auto()


# maps from operators to their meta functions
# declared here but updated below
ops_to_meta_functions_map = {}
ops_to_pretty_name_map = {}

# Prim defintions
# A primitive definition needs to do the following:
#   - define the op's meta function
#       The meta function maps proxy inputs to a proxy output that has the same metadata
#       as the result of calling the operation with inputs that have the same metadata as the proxies.
#       Meta functions are called within a tracing context. TODO: relax this.
#   - call make_prim

# TODO: add error context


_dataclass_params = {
    "frozen": True,
}
if sys.version_info >= (3, 10):
    _dataclass_params["slots"] = True


@dataclass(**_dataclass_params)
class Symbol:
    """A symbolic representation for the call to a primitive.

    Attributes:
        op: the operator enum
        name: the name of the operator
        outputs: the result of the operation
        args: the arguments to the operation
        kwargs: the keyword arguments to the operation
    """

    op: Enum = field(repr=False)
    name: str
    outputs: Tuple[Proxy]
    args: Tuple[Proxy]
    kwargs: Dict[str, Proxy]

    def __repr__(self):
        result_string = ", ".join(str(output) for output in self.outputs)
        arg_string = ", ".join(str(arg) for arg in self.args)
        kwarg_string = ", ".join(f"{k}={v}" for k, v in self.kwargs.items())
        return f"[Symbol {self.name}, \n\toutputs=({result_string}), \n\targs=({arg_string}), \n\tkwargs={{{kwarg_string}}}]"

    # Symbols are hashable and comparable by identity
    # This is necessary for using them as keys in a dict.
    # See symbols_to_region_map in thunder/executors/nvfuser.py for the usage.
    # TODO: if kwargs were hashable (frozendict), we could use a tuple of (op, args, kwargs) as the key
    #       and avoid the need for this.
    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other


def make_symbol(id, name, outputs, args, kwargs):
    """Creates a Symbol and adds it to the current trace.

    Prepares the arguments and outputs for the Symbol.

    Args:
        id: the operator enum
        name: the name of the operator
        outputs: the result of the operation
        args: the arguments to the operation
        kwargs: the keyword arguments to the operation

    Returns:
        The symbol.
    """
    # Normalize the outputs and args to tuples
    symbol_outputs = tuple(outputs) if isinstance(outputs, Sequence) else (outputs,)
    symbol_args = tuple(map(lambda a: tuple(a) if isinstance(a, Sequence) else a, args))
    symbol = Symbol(id, name, symbol_outputs, symbol_args, kwargs)
    return symbol


def eval_meta_and_record_symbol_fn(meta, id, name, *args, **kwargs):
    """Returns the result of the meta function and records a corresponding Symbol in the current trace.

    Args:
        meta: the meta function
        id: the operator enum
        name: the name of the operator
        args: the arguments to the operation
        kwargs: the keyword arguments to the operation

    Returns:
        The result of the meta function.
    """

    def _fn(*args, **kwargs):
        result = meta(*args, **kwargs)
        sym = make_symbol(id, name, result, args, kwargs)
        get_trace().add_symbol(sym)
        return result

    # TODO: update more of the signature
    _fn.__name__ = name

    return _fn


def make_prim(id, name, meta):
    # TODO: probably want to consolidate these maps by having one map
    #   to a prim data object with these attributes
    #   (or possibly to a Prim class and rename the class that is inserted into traces)
    ops_to_meta_functions_map[id] = meta
    ops_to_pretty_name_map[id] = name

    # TODO: update the signature
    return eval_meta_and_record_symbol_fn(meta, id, name)


#
# Data movement and transformation prims
#


# TODO: consider supporting number subclasses
def _convert_element_type_meta(a, dtype):
    if isinstance(a, Number):
        utils.check(utils.is_numbertype(dtype), lambda: f"Trying to convert a number to non-numbertype object {dtype}!")
        result = dtype(utils.get_numberlike_value(a))
        proxy_name = get_trace().make_proxy_name()
        return proxy(result, name=proxy_name)

    # a is a Tensor
    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(name=proxy_name, tensor=a, dtype=dtype)


convert_element_type = make_prim(Ops.CONVERT_ELEMENT_TYPE, "convert_element_type", _convert_element_type_meta)

#
# Tensor creation prims
#


# TODO: add some architecture for constructing tensor creation prims
# TODO: add device support to tensor proxies
def _full_meta(shape, fill_value, *, device, dtype):
    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(name=proxy_name, shape=shape, device=device, dtype=dtype)


full = make_prim(Ops.FULL, "full", _full_meta)


def _iota_meta(length, *, start, step, device, dtype):
    utils.check(utils.is_exact_dtype(dtype), lambda: f"dtype={dtype} was not an exact dtype")
    utils.check(not utils.is_boolean_dtype(dtype), lambda: f"dtype={dtype} was not a non-boolean dtype")
    utils.check(length >= 0, lambda: f"length={length} was not weakly positive")

    shape = () if length == 0 else (length,)

    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(name=proxy_name, shape=shape, device=device, dtype=dtype)


iota = make_prim(Ops.IOTA, "iota", _iota_meta)

# TODO: should the uniform prim include minval maxval or always be [0, 1)?
def _uniform_meta(shape, minval, maxval, *, device, dtype):
    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(name=proxy_name, shape=shape, device=device, dtype=dtype)


uniform = make_prim(Ops.UNIFORM, "uniform", _uniform_meta)

#
# Elementwise prims
#

# Describes how an elementwise primitive type promotes.
# NOTE: this is distinct from ELEMENTWISE_TYPE_PROMOTION_KIND in utils.py,
#   which describes how user-facing elementwise operations type promote.
# This type promotion just maps an input type to a result type.
# DEFAULT means the result type is the same as the input type.
# ALWAYS_BOOL means the result type is always bool.
# COMPLEX_TO_FLOAT means the result type is determined like for DEFAULT, unless
#   the input type is complex, in which case the result type is the corresponding
#   float type.
# Examples uses:
#  - DEFAULT: add
#  - ALWAYS_BOOL: isfinite
#  - COMPLEX_TO_FLOAT: abs


class ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND(Enum):
    DEFAULT = auto()
    ALWAYS_BOOL = auto()
    COMPLEX_TO_FLOAT = auto()


def _prim_type_promotion(typ, type_promotion_kind):
    if type_promotion_kind is ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT:
        return typ

    if type_promotion_kind is ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.ALWAYS_BOOL:
        return bool

    if type_promotion_kind is ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.COMPLEX_TO_FLOAT:
        if utils.is_complex_dtype(typ):
            return utils.corresponding_real_dtype(typ)

        return typ

    raise AssertionError("Unknown prim type promotion kind {type_promotion_kind}!")


#
# Elementwise unary prims
#

# Elementwise unary prims to implement:
# "asinh",
# "cbrt",
# "digamma",
# "erf_inv",
# "erfcx",
# "exp2",
# "fill",
# "imag",
# "lgamma",
# "ndtri",
# "neg",
# "real",
# "reciprocal",
# "round",
# "sign",
# "signbit",
# "sinh",
# "sqrt",
# "tan",
# "trunc",

# nvFuser unary ops (from https://github.com/pytorch/pytorch/blob/master/torch/_prims/nvfuser_prims.py)
# "imag",
# "lgamma",
# "reciprocal",
# "neg",
# "real",
# "round",
# "sign",
# "sinh",
# "sqrt",
# "tan",
# "trunc",

# TODO: review number handlers for complex support


def _elementwise_unary_meta(a, *, name, type_promotion_kind, number_handler=None, **kwargs):
    # TODO: break fn into two, one for returning types, one for checking for equality?
    input_dtype = utils.to_dtype(a, true_dtype=True)

    result_dtype = _prim_type_promotion(input_dtype, type_promotion_kind=type_promotion_kind)
    proxy_name = get_trace().make_proxy_name()

    # Tensor case
    if isinstance(a, TensorProxy):
        return TensorProxy(name=proxy_name, tensor=a, dtype=result_dtype)

    # Number case
    check(
        isinstance(a, Number),
        lambda: f"Elementwise unary primitives don't support inputs of type {type(a)}!",
    )

    check(
        number_handler is not None,
        lambda: f"The elementwise unary primitive {name} doesn't support number inputs!",
    )

    # a_typ = get_numberlike_type(a)
    va = get_numberlike_value(a)
    result = result_dtype(number_handler(va))
    return proxy(result, name=proxy_name)


abs = make_prim(
    Ops.ABS,
    "abs",
    partial(
        _elementwise_unary_meta,
        name="abs",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.COMPLEX_TO_FLOAT,
        number_handler=builtins.abs,
    ),
)

acos = make_prim(
    Ops.ACOS,
    "acos",
    partial(
        _elementwise_unary_meta,
        name="acos",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.acos,
    ),
)

acosh = make_prim(
    Ops.ACOSH,
    "acosh",
    partial(
        _elementwise_unary_meta,
        name="acosh",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.acosh,
    ),
)

asin = make_prim(
    Ops.ASIN,
    "asin",
    partial(
        _elementwise_unary_meta,
        name="asin",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.asin,
    ),
)

atan = make_prim(
    Ops.ATAN,
    "atan",
    partial(
        _elementwise_unary_meta,
        name="atan",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.atan,
    ),
)

atanh = make_prim(
    Ops.ATANH,
    "atanh",
    partial(
        _elementwise_unary_meta,
        name="atanh",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.atanh,
    ),
)

bitwise_not = make_prim(
    Ops.BITWISE_NOT,
    "bitwise_not",
    partial(
        _elementwise_unary_meta,
        name="bitwise_not",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=operator.invert,
    ),
)

ceil = make_prim(
    Ops.CEIL,
    "ceil",
    partial(
        _elementwise_unary_meta,
        name="ceil",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.ceil,
    ),
)

cos = make_prim(
    Ops.COS,
    "cos",
    partial(
        _elementwise_unary_meta,
        name="cos",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.cos,
    ),
)

cosh = make_prim(
    Ops.COSH,
    "cosh",
    partial(
        _elementwise_unary_meta,
        name="cosh",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.cosh,
    ),
)

erf = make_prim(
    Ops.ERF,
    "erf",
    partial(
        _elementwise_unary_meta,
        name="erf",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.erf,
    ),
)

erfc = make_prim(
    Ops.ERFC,
    "erfc",
    partial(
        _elementwise_unary_meta,
        name="erfc",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.erfc,
    ),
)

exp = make_prim(
    Ops.EXP,
    "exp",
    partial(
        _elementwise_unary_meta,
        name="exp",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.exp,
    ),
)

expm1 = make_prim(
    Ops.EXPM1,
    "expm1",
    partial(
        _elementwise_unary_meta,
        name="expm1",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.expm1,
    ),
)

floor = make_prim(
    Ops.FLOOR,
    "floor",
    partial(
        _elementwise_unary_meta,
        name="floor",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.floor,
    ),
)

isfinite = make_prim(
    Ops.ISFINITE,
    "isfinite",
    partial(
        _elementwise_unary_meta,
        name="isfinite",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.ALWAYS_BOOL,
        number_handler=math.isfinite,
    ),
)

# TODO: improve this
def _rsqrt_number(x):
    return 1 / math.sqrt(x)


rsqrt = make_prim(
    Ops.RSQRT,
    "rsqrt",
    partial(
        _elementwise_unary_meta,
        name="rsqrt",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.ALWAYS_BOOL,
        number_handler=_rsqrt_number,
    ),
)

sin = make_prim(
    Ops.SIN,
    "sin",
    partial(
        _elementwise_unary_meta,
        name="sin",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.sin,
    ),
)

tanh = make_prim(
    Ops.TANH,
    "tanh",
    partial(
        _elementwise_unary_meta,
        name="tanh",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.tanh,
    ),
)

log = make_prim(
    Ops.LOG,
    "log",
    partial(
        _elementwise_unary_meta,
        name="log",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.log,
    ),
)

log10 = make_prim(
    Ops.LOG10,
    "log10",
    partial(
        _elementwise_unary_meta,
        name="log10",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.log10,
    ),
)

log1p = make_prim(
    Ops.LOG1P,
    "log1p",
    partial(
        _elementwise_unary_meta,
        name="log1p",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.log1p,
    ),
)

log2 = make_prim(
    Ops.LOG2,
    "log2",
    partial(
        _elementwise_unary_meta,
        name="log2",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=math.log2,
    ),
)

# "log1p",
# "log2",
# "log10",

#
# Elementwise binary prims
#
# "bitwise_or",
# "bitwise_xor",
# # 'complex',  # needs custom meta
# "eq",
# "fmax",
# "fmin",
# "fmod",
# "gcd",
# "ge",
# "gt",
# "hypot",
# "igamma",
# "igammac",
# "le",
# "maximum",
# "minimum",
# "ne",
# "nextafter",
# "remainder",
# "shift_left",
# "shift_right_arithmetic",
# "shift_right_logical",  # not implemented
# "zeta",

# nvFuser binary ops (from https://github.com/pytorch/pytorch/blob/master/torch/_prims/nvfuser_prims.py)
# "bitwise_or",
# "bitwise_xor",
# "eq",
# "fmod",
# "ge",
# "gt",
# "le",
# "ne",
# "remainder",


# TODO: add type promotion (ex. abs complex->float type promotion)
# TODO: document elementwise binary meta, incl. stride logic
# TODO: use supported_dtypes
# TODO: correct name of output
def _elementwise_binary_meta(
    a, b, *, name, type_promotion_kind, number_handler=None, supported_dtypes=(dtypes.dtype,), **kwargs
):
    # Tensors or Number inputs only
    if not isinstance(a, (TensorProxy, Number)):
        raise ValueError(f"Unexpected type {type(a)}!")
    if not isinstance(b, (TensorProxy, Number)):
        raise ValueError(f"Unexpected type {type(b)}!")

    # Inputs must have the same dtype
    numbertype, dtype = utils.check_same_dtype(a, b)
    input_type = dtype if dtype is not None else numbertype

    result_type = _prim_type_promotion(input_type, type_promotion_kind=type_promotion_kind)
    proxy_name = get_trace().make_proxy_name()

    # tensor x tensor case
    if isinstance(a, TensorProxy) and isinstance(b, TensorProxy):
        check(
            same_shape(a.shape, b.shape),
            lambda: (
                "Elementwise binary primitives require the shapes of the inputs tensors to "
                f"be the same! But got shapes {a.shape} and {b.shape}!"
            ),
        )

        return TensorProxy(name=proxy_name, tensor=a, dtype=result_type)

    # scalar x scalar case
    if isinstance(a, Number) and isinstance(b, Number):
        check(
            number_handler is not None,
            lambda: f"The elementwise binary primitive {name} doesn't support number x number inputs!",
        )

        va, vb = get_numberlike_value(a), get_numberlike_value(b)
        value = number_handler(va, vb)
        result = result_type(value)
        return proxy(result, name=proxy_name)

    # tensor x scalar case
    tensor = a if isinstance(a, TensorProxy) else b

    return TensorProxy(name=proxy_name, tensor=tensor, dtype=result_type)


add = make_prim(
    Ops.ADD,
    "add",
    partial(
        _elementwise_binary_meta,
        name="add",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=operator.add,
    ),
)

atan2 = make_prim(
    Ops.ATAN2,
    "atan2",
    partial(
        _elementwise_binary_meta,
        name="atan2",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
    ),
)

bitwise_and = make_prim(
    Ops.BITWISE_AND,
    "bitwise_and",
    partial(
        _elementwise_binary_meta,
        name="bitwise_and",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        supported_dtypes=(dtypes.exact,),
    ),
)


def _div_number_handler(a, b):
    if isinstance(a, (float, complex)):
        return a / b

    # int (and bool) case, performs floor division
    return a // b


div = make_prim(
    Ops.DIV,
    "div",
    partial(
        _elementwise_binary_meta,
        name="div",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=_div_number_handler,
    ),
)

eq = make_prim(
    Ops.EQ,
    "eq",
    partial(
        _elementwise_binary_meta,
        name="eq",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.ALWAYS_BOOL,
        number_handler=operator.eq,
    ),
)

lt = make_prim(
    Ops.LT,
    "lt",
    partial(
        _elementwise_binary_meta,
        name="lt",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.ALWAYS_BOOL,
        number_handler=operator.lt,
    ),
)

mul = make_prim(
    Ops.MUL,
    "mul",
    partial(
        _elementwise_binary_meta,
        name="mul",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=operator.mul,
    ),
)

pow = make_prim(
    Ops.POW,
    "pow",
    partial(
        _elementwise_binary_meta,
        name="pow",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=operator.pow,
    ),
)

sub = make_prim(
    Ops.SUB,
    "sub",
    partial(
        _elementwise_binary_meta,
        name="sub",
        type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT,
        number_handler=operator.sub,
    ),
)

#
# Elementwise ternary prims
#

# TODO: add stride logic
def where_meta(pred, a, b):
    # Checks types
    # NOTE: pred must be a tensor or bool
    utils.check(isinstance(pred, (TensorProxy, bool)), lambda: f"Unexpected type {type(pred)} for pred={pred}!")
    utils.check(isinstance(a, (TensorProxy, Number)), lambda: f"Unexpected type {type(a)} for a={a}!")
    utils.check(isinstance(b, (TensorProxy, Number)), lambda: f"Unexpected type {type(b)} for b={b}!")

    # Checks devices and determines result device
    utils.check_same_device(pred, a, b)
    resultdevice = "cpu"
    devices = tuple(x.device for x in (pred, a, b) if isinstance(x, TensorProxy))
    if len(devices) > 0:
        resultdevice = devices[0]

    # Checks pred dtype and determines result dtype
    utils.check(
        isinstance(pred, bool) or pred.dtype is dtypes.bool8,
        lambda: f"Expected pred to have a bool dtype, but found {type(pred) if isinstance(pred, Number) else pred.dtype}!",
    )
    numbertype, tensordtype = utils.check_same_dtype(a, b)
    dtype = tensordtype if tensordtype is not None else numbertype
    resulttype = _prim_type_promotion(dtype, type_promotion_kind=ELEMENTWISE_PRIM_TYPE_PROMOTION_KIND.DEFAULT)

    # Checks shapes
    utils.check_same_shape(pred, a, b)

    # Constructs return meta
    proxyname = get_trace().make_proxy_name()

    # Handles all number case with customer number handler
    if isinstance(pred, Number) and isinstance(a, Number) and isinstance(b, Number):
        result = a if pred else b
        result = resulttype(result)
        return proxy(result, name=proxyname)

    # Determines output shape
    resultshape = None
    shapes = tuple(x.shape for x in (pred, a, b) if isinstance(x, TensorProxy))
    if len(shapes) > 0:
        resultshape = shapes[0]

    return TensorProxy(name=proxyname, shape=resultshape, device=resultdevice, dtype=resulttype)


where = make_prim(Ops.WHERE, "where", where_meta)

#
# Shape prims
#


# TODO: may want to update these error types
# NOTE: broadcast_dimensions is a sequence with length equal to a.shape
def broadcast_in_dim_meta(a, shape, broadcast_dimensions, **kwargs):
    utils.check(
        len(a.shape) == len(broadcast_dimensions),
        lambda: f"Expected one broadcast dimension (broadcast_dimensions={broadcast_dimensions}) for each dimension of a={a.shape}",
    )

    # Checks that dimensions are strictly increasing and valid
    prev_idx = -1
    for original_length, idx in zip(a.shape, broadcast_dimensions):
        utils.check(
            idx > prev_idx,
            lambda: f"Expected the dimensions in broadcast_dimensions={broadcast_dimensions} to be strictly increasing",
        )
        prev_idx = idx

        utils.check(
            idx < len(shape),
            lambda: f"One of the broadcast_dimensions={broadcast_dimensions} was {idx}, which is out-of-bounds for a tensor with {len(shape)} dimensions",
        )
        utils.check(
            original_length == 1 or shape[idx] == original_length,
            lambda: f"A dimension of length {original_length} cannot be broadcast to a dimension of length {shape[idx]}",
        )

    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(name=proxy_name, shape=shape, device=a.device, dtype=a.true_dtype)


broadcast_in_dim = make_prim(
    Ops.BROADCAST_IN_DIM,
    "broadcast_in_dim",
    broadcast_in_dim_meta,
)


def reshape_meta(a, shape):
    # Validates inputs
    utils.check(isinstance(a, TensorProxy), lambda: f"a={a} was not a TensorProxy!")
    utils.check_valid_shape(shape)
    numel = reduce(operator.mul, shape, 1)
    utils.check(
        numel == a.numel(),
        lambda: f"Attempting to reshape a.shape={a.shape} to shape={shape}, but a.numel()={a.numel()} is different from the number of elements in shape, {numel}",
    )

    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(tensor=a, name=proxy_name, shape=shape)


reshape = make_prim(
    Ops.RESHAPE,
    "reshape",
    reshape_meta,
)

# TODO: be clear about what the prim can handle and what it can't
# NOTE: the stride parameter here refers to the stride of the slice, not the tensor's
#   strides
def slice_meta(a, start_indices, end_indices, strides=None):
    if strides is None:
        strides = [1] * a.ndim

    # Checks types
    utils.check(isinstance(a, TensorProxy), lambda: f"Expected a={a} to be a TensorProxy!")
    utils.check(isinstance(start_indices, Sequence), lambda: f"Expected start_indices={start_indices} to be a Sequence")
    utils.check(isinstance(end_indices, Sequence), lambda: f"Expected end_indices={end_indices} to be a Sequence")
    utils.check(isinstance(strides, Sequence), lambda: f"Expected strides={strides} to be None or a Sequence")

    # Checks all same length
    utils.check(
        a.ndim == len(start_indices) == len(end_indices) == len(strides),
        lambda: f"Expected the tensor's rank ({a.ndim}) to be equal to the length of start_indices ({len(start_indices)}), the length of end_indices ({len(end_indices)}), and the length of strides ({len(strides)})",
    )

    # Validates start, end, and stride values, and computes the new shape
    new_shape = []
    for start, stop, shape, stride in zip(start_indices, end_indices, a.shape, strides):
        utils.check(
            start >= 0, lambda: f"Expected all the indices in start_indices={start_indices} to be weakly positive!"
        )
        utils.check(
            start <= shape,
            lambda: f"Expected all the indices in start_indices={start_indices} to be weakly less than the length of the corresponding dimension in a.shape={a.shape}",
        )
        utils.check(
            start <= stop,
            lambda: f"Expected all the indices in start_indices={start_indices} to be weakly less than the indices in end_indices={end_indices}",
        )
        utils.check(
            stop <= shape,
            lambda: f"Expected all the indices in end_indices={end_indices} to be weakly less than the length of the corresponding dimension in a.shape={a.shape}",
        )
        utils.check(stride >= 1, lambda: f"Expected all the strides in strides={strides} to be strictly positive!")

        new_shape.append(math.floor((stop - start) / stride))

    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(tensor=a, name=proxy_name, shape=new_shape)


# NOTE: slice is named "slice_prim" and not "slice" because it conflicts with Python's "slice" builtin
slice_prim = make_prim(Ops.SLICE, "slice", slice_meta)


def squeeze_meta(a, dims):
    # Checks that no dims are redundant
    utils.check_no_duplicates(dims)

    # Checks that dims are valid
    for x in dims:
        utils.check(
            x >= 0 and x < len(a.shape), lambda: f"dims={dims} contained an invalid dimension {x} for a.shape={a.shape}"
        )

    shape = []
    for idx, l in enumerate(a.shape):
        # Checks that squeezed dims have length one
        if idx in dims:
            utils.check(l == 1, lambda: f"Cannot squeeze dimension {idx} of length {l} in a.shape={a.shape}")
            continue

        shape.append(l)

    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(tensor=a, name=proxy_name, shape=shape)


squeeze = make_prim(Ops.SQUEEZE, "squeeze", squeeze_meta)


def transpose_meta(a, permutation):
    utils.check(isinstance(a, TensorProxy), lambda: f"Expected a={a} to be a TensorProxy!")
    utils.check(
        a.ndim == len(permutation),
        lambda: f"Expected the length ({len(permutation)}) of the permutation={permutation} to be the number of dimensions ({a.ndim}) of a={a}",
    )
    utils.check_valid_permutation(a.ndim, permutation)

    new_shape = [0] * a.ndim
    for idx, dim in enumerate(permutation):
        new_shape[idx] = a.shape[dim]

    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(tensor=a, name=proxy_name, shape=new_shape)


transpose = make_prim(Ops.TRANSPOSE, "transpose", transpose_meta)

view = make_prim(Ops.VIEW, "view", reshape_meta)

#
# Reduction prims
#


def _compute_reduction_output_shape(shape, dims):
    for idx in dims:
        utils.validate_idx(len(shape), idx)

    new_shape = []
    for idx in range(len(shape)):
        if idx in dims:
            continue

        new_shape.append(shape[idx])

    return tuple(new_shape)


def reduction_meta(a, dims, *, output_dtype=None, **kwargs):
    """Meta function for single output reduction operations."""

    if output_dtype is None:
        output_dtype = a.true_dtype

    output_shape = _compute_reduction_output_shape(a.shape, dims)

    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(
        name=proxy_name,
        shape=output_shape,
        device=a.device,
        dtype=output_dtype,
    )


# TODO: review if reduction meta is OK for amax
amax = make_prim(Ops.AMAX, "amax", reduction_meta)
sum = make_prim(Ops.SUM, "sum", reduction_meta)


def var_meta(a, dims, *, correction, **kwargs):
    output_dtype = None
    if utils.is_complex_dtype(a.dtype):
        output_dtype = utils.corresponding_real_dtype(a.true_dtype)
    else:
        output_dtype = a.true_dtype
    return reduction_meta(a, dims, output_dtype=output_dtype)


var = make_prim(Ops.VAR, "var", var_meta)

#
# Matmul prims
#
# NOTE: matmul prims are highly experimental and will almost definitely change

# out = a @ w.transpose() + bias
def linear_meta(a, w, bias):
    # a's shape is (batch dims..., in)
    # w's shape is (out x in)
    # if bias is not None, bias's shape is (out)
    # the output shape is (batch dims..., out)

    # Checks types of the required arguments
    utils.check(isinstance(a, TensorProxy), lambda: f"a={a} was not a TensorProxy!")
    utils.check(isinstance(w, TensorProxy), lambda: f"w={w} was not a TensorProxy!")

    # Checks that required arguments are on the same device
    utils.check(a.device == w.device, lambda: f"Expected a.device={a.device} and w.device={w.device} to be the same!")

    # Acquires the computation dtype and checks that a and w have the same dtype
    dtype = a.dtype
    utils.check(
        dtypes.are_same_dtypes(a, w), lambda: f"Expected a.dtype={a.dtype} and w.dtype={w.dtype} to be the same!"
    )

    # Acquires the shape information and validates the shapes of the required arguments
    batch_dims = a.shape[:-1]
    in_length = a.shape[-1]

    # Validates w's shape
    utils.check(
        len(w.shape) == 2, lambda: f"Expected w.shape={w.shape} to have length 2, but found length {len(w.shape)}!"
    )
    utils.check(
        w.shape[1] == in_length,
        lambda: f"Expected w.shape={w.shape} to have an innermost dimension of length {in_length}, the same length as the innermost dimension of a.shape={a.shape}!",
    )

    out_length = w.shape[0]

    # Validates bias shape
    if bias is not None:
        utils.check(isinstance(bias, TensorProxy), lambda: f"bias={bias} was not None or a TensorProxy!")
        utils.check(
            a.device == bias.device,
            lambda: f"Expected a.device={a.device} and bias.device={bias.device} to be the same!",
        )
        utils.check(
            len(bias.shape) == 1,
            lambda: f"Expected bias.shape={bias.shape} to have length 1, but found length {len(bias.shape)}!",
        )
        utils.check(
            bias.shape[0] == out_length,
            lambda: f"Expected bias.shape={bias.shape} to have an innermost dimension of length {out_length}, the same length as the outermost dimension of w.shape={w.shape}!",
        )
        utils.check(
            dtypes.are_same_dtypes(bias, a),
            lambda: f"Expected a.dtype={a.dtype} and bias.dtype={bias.dtype} to be the same!",
        )

    out_shape = batch_dims + (out_length,)
    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(name=proxy_name, shape=out_shape, device=a.device, dtype=dtype)


linear = make_prim(Ops.LINEAR, "linear", linear_meta)


# TODO: review matmul prims
def matmul_meta(a, b):
    # Checks types
    utils.check(isinstance(a, TensorProxy), lambda: f"a={a} was not a TensorProxy")
    utils.check(isinstance(b, TensorProxy), lambda: f"b={b} was not a TensorProxy")

    if a.ndim < 2 or b.ndim < 2:
        raise NotImplemented

    utils.check(a.device == b.device, lambda: f"Expected a.device={a.device} and b.device={b.device} to be the same")

    utils.check(
        dtypes.are_same_dtypes(a, b), lambda: f"Expected a.dtype={a.dtype} and b.dtype={b.dtype} to be the same"
    )

    utils.check(
        utils.same_shape(a.shape[:-2], b.shape[:-2]),
        lambda: f"Expected the batch dimensions of a ({a.shape[:-2],}) and the batch dimensions of b ({b.shape[:-2]}) to be the same",
    )

    utils.check(
        a.shape[-1] == b.shape[-2],
        lambda: f"Expected the the last two dimensions of a ({a.shape[-2:]}) be matrix multipiable with the last two dimensions of b ({b.shape[-2:]})",
    )

    shape = list(a.shape[:-2])
    shape.append(a.shape[-2])
    shape.append(b.shape[-1])
    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(name=proxy_name, shape=shape, device=a.device, dtype=a.dtype)


matmul = make_prim(Ops.MATMUL, "matmul", matmul_meta)

#
# NN prims
#

# TODO: these require review


def embedding_meta(a, weight, padding_idx=-1, max_norm=None, norm_type=2.0, scale_grad_by_freq=False, sparse=False):
    # TODO: canonicalize and validating padding idx with weight.shape[0]

    if max_norm is not None:
        raise NotImplemented

    utils.check(a.dtype == dtypes.int64, lambda: f"Expected a.dtype={a.dtype} to be int64")
    utils.check(weight.ndim == 2, lambda: f"Expected weight (weight.shape={weight.shape} to be a matrix)")

    shape = list(a.shape)
    shape.append(weight.shape[1])

    proxy_name = get_trace().make_proxy_name()
    return TensorProxy(name=proxy_name, shape=shape, device=weight.device, dtype=weight.dtype)


embedding = make_prim(Ops.EMBEDDING, "embedding", embedding_meta)