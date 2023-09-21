# Copyright 2023 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This module provides the implementation of AutoGraph primitives in terms of traceable Catalyst
functions. The purpose is to convert imperative style code to functional or graph-style code."""

import functools
import warnings
from typing import Any, Callable, Iterator, SupportsIndex, Tuple

import jax
import jax.numpy as jnp

# Use tensorflow implementations for handling function scopes and calls,
# as well as various utility objects.
import pennylane as qml
import tensorflow.python.autograph.impl.api as tf_autograph_api
from tensorflow.python.autograph.core import config
from tensorflow.python.autograph.core.converter import STANDARD_OPTIONS as STD
from tensorflow.python.autograph.core.converter import ConversionOptions
from tensorflow.python.autograph.core.function_wrappers import (
    FunctionScope,
    with_function_scope,
)
from tensorflow.python.autograph.impl.api import autograph_artifact
from tensorflow.python.autograph.impl.api import converted_call as tf_converted_call
from tensorflow.python.autograph.operators.variables import (
    Undefined,
    UndefinedReturnValue,
)
from tensorflow.python.autograph.pyct.origin_info import LineLocation

import catalyst
from catalyst.ag_utils import AutoGraphError
from catalyst.utils.patching import Patcher

__all__ = [
    "STD",
    "ConversionOptions",
    "Undefined",
    "UndefinedReturnValue",
    "autograph_artifact",
    "FunctionScope",
    "with_function_scope",
    "if_stmt",
    "for_stmt",
    "converted_call",
]


def assert_results(results, var_names):
    """Assert that none of the results are undefined, i.e. have no value."""

    assert len(results) == len(var_names)

    for r, v in zip(results, var_names):
        if isinstance(r, Undefined):
            raise AutoGraphError(f"Some branches did not define a value for variable '{v}'")

    return results


# pylint: disable=too-many-arguments
def if_stmt(
    pred: bool,
    true_fn: Callable[[], Any],
    false_fn: Callable[[], Any],
    get_state: Callable[[], Tuple],
    set_state: Callable[[Tuple], None],
    symbol_names: Tuple[str],
    _num_results: int,
):
    """An implementation of the AutoGraph 'if' statement. The interface is defined by AutoGraph,
    here we merely provide an implementation of it in terms of Catalyst primitives."""

    # Cache the initial state of all modified variables. Required because we trace all branches,
    # and want to restore the initial state before entering each branch.
    init_state = get_state()

    @catalyst.cond(pred)
    def functional_cond():
        set_state(init_state)
        true_fn()
        results = get_state()
        return assert_results(results, symbol_names)

    @functional_cond.otherwise
    def functional_cond():
        set_state(init_state)
        false_fn()
        results = get_state()
        return assert_results(results, symbol_names)

    # Sometimes we unpack the results of nested tracing scopes so that the user doesn't have to
    # manipulate tuples when they don't expect it. Ensure set_state receives a tuple regardless.
    results = functional_cond()
    if not isinstance(results, tuple):
        results = (results,)
    set_state(results)


def assert_for_loop_inputs(inputs, iterate_names):
    """All loop carried values, variables that are updated each iteration or accessed after the
    loop terminates, need to be initialized prior to entering the loop.

    The reason is two-fold:
      - the type information from those variables is required for tracing
      - we want to avoid access of a variable that is uninitialized, or uninitialized in a subset
        of execution paths

    Additionally, these types need to be valid JAX types.
    """

    for i, inp in enumerate(inputs):
        if isinstance(inp, Undefined):
            raise AutoGraphError(
                f"The variable '{inp}' is potentially uninitialized:\n"
                " - you may have forgotten to initialize it prior to accessing it inside a loop, or"
                "\n"
                " - you may be attempting to access a variable local to the body of a loop in an "
                "outer scope.\n"
                f"Please ensure '{inp}' is initialized with a value before entering the loop."
            )

        try:
            jax.api_util.shaped_abstractify(inp)
        except TypeError as e:
            raise AutoGraphError(
                f"The variable '{iterate_names[i]}' was initialized with type {type(inp)}, "
                "which is not compatible with JAX. Typically, this is the case for non-numeric "
                "values.\n"
                "You may still use such a variable as a constant inside a loop, but it cannot "
                "be updated from one iteration to the next, or accessed outside the loop scope "
                "if it was defined inside of it."
            ) from e


def assert_for_loop_results(inputs, outputs, iterate_names):
    """The results of a for loop should have the identical type as the inputs, since they are
    "passed" as inputs to the next iteration. A mismatch here may indicate that a loop carried
    variable was initialized with wrong type.
    """

    for i, (inp, out) in enumerate(zip(inputs, outputs)):
        inp_t, out_t = jax.api_util.shaped_abstractify(inp), jax.api_util.shaped_abstractify(out)
        if inp_t.dtype != out_t.dtype or inp_t.shape != out_t.shape:
            raise AutoGraphError(
                f"The variable '{iterate_names[i]}' was initialized with the wrong type. "
                f"Expected: {out_t}, Got: {inp_t}"
            )


def _call_catalyst_for(
    start, stop, step, body_fn, get_state, set_state, opts, enum_start=None, array_iterable=None
):
    """Dispatch to a Catalyst implementation of for loops."""

    # Ensure iteration arguments are properly initialized. We cannot process uninitialized
    # loop carried values as we need their type information for tracing.
    init_iter_args = get_state()
    assert_for_loop_inputs(init_iter_args, opts["iterate_names"])

    @catalyst.for_loop(start, stop, step)
    def functional_for(i, *iter_args):
        # Assign tracers to the iteration variables identified by AutoGraph (iter_args in mlir).
        set_state(iter_args)

        # The iteration index/element (for <...> in) is already handled by the body function, e.g.:
        #   def body_fn(itr):
        #     i, x = itr
        #     ...
        if enum_start is None and array_iterable is None:
            # for i in range(..)
            body_fn(i)
        elif enum_start is None:
            # for x in array
            body_fn(array_iterable[i])
        else:
            # for (i, x) in enumerate(array)
            body_fn((i + enum_start, array_iterable[i]))

        return get_state()

    final_iter_args = functional_for(*init_iter_args)
    assert_for_loop_results(init_iter_args, final_iter_args, opts["iterate_names"])
    return final_iter_args


def _call_python_for(body_fn, get_state, non_array_iterable):
    """Fallback to a Python implementation of for loops."""

    for elem in non_array_iterable:
        body_fn(elem)

    return get_state()


def for_stmt(
    iteration_target: Any,
    _extra_test: Callable[[], bool] | None,
    body_fn: Callable[[int], None],
    get_state: Callable[[], Tuple],
    set_state: Callable[[Tuple], None],
    _symbol_names: Tuple[str],
    opts: dict,
):
    """An implementation of the AutoGraph 'for .. in ..' statement. The interface is defined by
    AutoGraph, here we merely provide an implementation of it in terms of Catalyst primitives."""

    assert _extra_test is None

    # The general approach is to convert as much code as possible into a graph-based form:
    # - For loops over iterables will attempt a conversion of the iterable to array, and fall back
    #   to Python otherwise.
    # - For loops over a Python range will be converted to a native Catalyst for loop. However,
    #   since the now dynamic iteration variable can cause issues in downstream user code, any
    #   errors raised during the tracing of the loop body will restart the tracing process using
    #   a Python loop instead.
    # - For loops over a Python enumeration use a combination of the above, providing a dynamic
    #   iteration variable and conversion of the iterable to array. If either fails, a fallback to
    #   Python is used.
    # Note that there are two reasons a fallback to Python could have been triggered:
    # - the iterable provided by the user is not convertible to an array
    #   -> this will fallback to a Python loop silently (without a warning), since there isn't a
    #      simple fix to make this loop traceable
    # - an exception is raised during the tracing of the loop body after conversion
    #   -> this will raise a warning to allow users to correct mistakes and allow the conversion
    #      to succeed, for example because they forgot to use a list instead of an array
    fallback = False
    init_state = get_state()

    if isinstance(iteration_target, CRange):
        start, stop, step = iteration_target.get_raw_range()
        enum_start = None
        iteration_array = None
    elif isinstance(iteration_target, CEnumerate):
        start, stop, step = 0, len(iteration_target.iteration_target), 1
        enum_start = iteration_target.start_idx
        try:
            iteration_array = jnp.asarray(iteration_target.iteration_target)
        except:  # pylint: disable=bare-except
            iteration_array = None
            fallback = True
    else:
        start, stop, step = 0, len(iteration_target), 1
        enum_start = None
        try:
            iteration_array = jnp.asarray(iteration_target)
        except:  # pylint: disable=bare-except
            iteration_array = None
            fallback = True

    if catalyst.autograph_strict_conversion and fallback:
        # pylint: disable=import-outside-toplevel
        import inspect

        for_loop_info = get_source_code_info(inspect.stack()[1])

        raise AutoGraphError(
            f"Could not convert the iteration target {iteration_target} to array while processing "
            f"the following with AutoGraph:\n{for_loop_info}"
        )

    # Attempt to trace the Catalyst for loop.
    if not fallback:
        try:
            set_state(init_state)
            results = _call_catalyst_for(
                start, stop, step, body_fn, get_state, set_state, opts, enum_start, iteration_array
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            if catalyst.autograph_strict_conversion:
                raise e

            # pylint: disable=import-outside-toplevel
            import inspect
            import textwrap

            fallback = True

            for_loop_info = get_source_code_info(inspect.stack()[1])

            if not catalyst.autograph_ignore_fallbacks:
                warnings.warn(
                    f"Tracing of an AutoGraph converted for loop failed with an exception:\n"
                    f"  {type(e).__name__}:{textwrap.indent(str(e), '    ')}\n"
                    f"\n"
                    f"The error ocurred within the body of the following for loop statement:\n"
                    f"{for_loop_info}"
                    f"\n"
                    f"If you intended for the conversion to happen, make sure that the (now "
                    f"dynamic) loop variable is not used in tracing-incompatible ways, for "
                    f"instance by indexing a Python list with it. In that case, the list should be "
                    f"wrapped into an array.\n"
                    f"To understand different types of JAX tracing errors, please refer to the "
                    f"guide at: https://jax.readthedocs.io/en/latest/errors.html\n"
                    f"\n"
                    f"If you did not intend for the conversion to happen, you may safely ignore "
                    f"this warning."
                )

    # If anything goes wrong, we fall back to Python.
    if fallback:
        set_state(init_state)
        results = _call_python_for(body_fn, get_state, iteration_target)

    # Sometimes we unpack the results of nested tracing scopes so that the user doesn't have to
    # manipulate tuples when they don't expect it. Ensure set_state receives a tuple regardless.
    if not isinstance(results, tuple):
        results = (results,)
    set_state(results)


def get_source_code_info(tb_frame):
    """Attempt to obtain original source code information for an exception raised within AutoGraph
    transformed code.

    Uses introspection on the call stack to extract the source map record from within AutoGraph
    statements. However, it is not guaranteed to find the source map and may return nothing.
    """
    import inspect  # pylint: disable=import-outside-toplevel

    ag_source_map = None

    # Traverse frames in reverse to find caller with `ag_source_map` property:
    # - function: directly on the callable object
    # - qnode method: on the self object
    # - qjit method: on the self.user_function object
    try:
        for frame in inspect.stack():
            if frame.function == "converted_call" and "converted_f" in frame.frame.f_locals:
                obj = frame.frame.f_locals["converted_f"]
                ag_source_map = obj.ag_source_map
                break
            if "self" in frame.frame.f_locals:
                obj = frame.frame.f_locals["self"]
                if isinstance(obj, qml.QNode):
                    ag_source_map = obj.ag_source_map
                    break
                if isinstance(obj, catalyst.QJIT):
                    ag_source_map = obj.user_function.ag_source_map
                    break
    except:  # nosec B110 # pylint: disable=bare-except # pragma: nocover
        pass

    loc = LineLocation(tb_frame.filename, tb_frame.lineno)
    if ag_source_map is not None and loc in ag_source_map:
        function_name = ag_source_map[loc].function_name
        filename = ag_source_map[loc].loc.filename
        lineno = ag_source_map[loc].loc.lineno
        source_code = ag_source_map[loc].source_code_line.strip()
    else:
        function_name = tb_frame.name
        filename = tb_frame.filename
        lineno = tb_frame.lineno
        source_code = tb_frame.line

    info = f'  File "{filename}", line {lineno}, in {function_name}\n' f"    {source_code}\n"

    return info


# Prevent autograph from converting PennyLane and Catalyst library code, this can lead to many
# issues such as always tracing through code that should only be executed conditionally. We might
# have to be even more restrictive in the future to prevent issues if necessary.
module_allowlist = (
    config.DoNotConvert("pennylane"),
    config.DoNotConvert("catalyst"),
    config.DoNotConvert("jax"),
) + config.CONVERSION_RULES


def converted_call(fn, args, kwargs, caller_fn_scope=None, options=None):
    """We want AutoGraph to use our own instance of the AST transformer when recursively
    transforming functions, but otherwise duplicate the same behaviour."""

    with Patcher(
        (tf_autograph_api, "_TRANSPILER", catalyst.autograph.TRANSFORMER),
        (config, "CONVERSION_RULES", module_allowlist),
    ):
        # Dispatch range calls to a custom range class that enables constructs like
        # `for .. in range(..)` to be converted natively to `for_loop` calls. This is beneficial
        # since the Python range function does not allow tracers as arguments.
        if fn is range:
            return CRange(*args, **(kwargs if kwargs is not None else {}))
        elif fn is enumerate:
            return CEnumerate(*args, **(kwargs if kwargs is not None else {}))

        # We need to unpack nested QNode and QJIT calls as autograph will have trouble handling
        # them. Ideally, we only want the wrapped function to be transformed by autograph, rather
        # than the QNode or QJIT call method.

        # For nested QJIT calls, the class already forwards to the wrapped function, bypassing any
        # class functionality. We just do the same here:
        if isinstance(fn, catalyst.QJIT):
            fn = fn.user_function

        # For QNode calls, we employ a wrapper to correctly forward the quantum function call to
        # autograph, while still invoking the QNode call method in the surrounding tracing context.
        if isinstance(fn, qml.QNode):

            @functools.wraps(fn.func)
            def qnode_call_wrapper():
                return tf_converted_call(fn.func, args, kwargs, caller_fn_scope, options)

            new_qnode = qml.QNode(qnode_call_wrapper, device=fn.device, diff_method=fn.diff_method)
            return new_qnode()

        return tf_converted_call(fn, args, kwargs, caller_fn_scope, options)


class CRange:
    """Catalyst range object.

    Can be passed to a Python for loop for native conversion to a for_loop call.
    Otherwise this class behaves exactly like the Python range class.

    Without this native conversion, all iteration targets in a Python for loop must be convertible
    to arrays. For all other inputs the loop will be treated as a regular Python loop.
    """

    def __init__(self, start_stop, stop=None, step=None):
        self._py_range = None
        self._start = start_stop if stop is not None else 0
        self._stop = stop if stop is not None else start_stop
        self._step = step if step is not None else 1

    def get_raw_range(self):
        """Get the raw values defining this range: start, stop, step."""
        return self._start, self._stop, self._step

    @property
    def py_range(self):
        """Access the underlying Python range object. If it doesn't exist, create one."""
        if self._py_range is None:
            self._py_range = range(self._start, self._stop, self._step)
        return self._py_range

    # Interface of the Python range class.
    # pylint: disable=missing-function-docstring

    @property
    def start(self) -> int:  # pragma: nocover
        return self.py_range.start

    @property
    def stop(self) -> int:  # pragma: nocover
        return self.py_range.stop

    @property
    def step(self) -> int:  # pragma: nocover
        return self.py_range.step

    def count(self, __value: int) -> int:  # pragma: nocover
        return self.py_range.count(__value)

    def index(self, __value: int) -> int:  # pragma: nocover
        return self.py_range.index(__value)

    def __len__(self) -> int:  # pragma: nocover
        return self.py_range.__len__()

    def __eq__(self, __value: object) -> bool:  # pragma: nocover
        return self.py_range.__eq__(__value)

    def __hash__(self) -> int:  # pragma: nocover
        return self.py_range.__hash__()

    def __contains__(self, __key: object) -> bool:  # pragma: nocover
        return self.py_range.__contains__(__key)

    def __iter__(self) -> Iterator[int]:  # pragma: nocover
        return self.py_range.__iter__()

    def __getitem__(self, __key: SupportsIndex | slice) -> int | range:  # pragma: nocover
        return self.py_range.__getitem__(__key)

    def __reversed__(self) -> Iterator[int]:  # pragma: nocover
        return self.py_range.__reversed__()


class CEnumerate(enumerate):
    """Catalyst enumeration object.

    Can be passed to a Python for loop for conversion into a for_loop call. The loop index, as well
    as the iterable element will be provided to the loop body.
    Otherwise this class behaves exactly like the Python enumerate class.

    Note that the iterable must be convertible to an array, otherwise the loop will be treated as a
    regular Python loop.
    """

    def __init__(self, iterable, start=0):
        self.iteration_target = iterable
        self.start_idx = start