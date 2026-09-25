"""Execute explicit COMSOL Java calls with factual reads around each change.

The method families follow the COMSOL 6.3 Model, AbstractModel, PropFeature,
Selection, LocalSelection and ModelEntityList interfaces. The installed COMSOL
version remains the authority for concrete properties and overloads.
"""

from __future__ import annotations

import json
import math
from typing import Any



API_REFERENCE_URLS = (
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/AbstractModel.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/PropFeature.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/Selection.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/LocalSelection.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/Material.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/ModelEntityList.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/ExpressionBase.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/ParamBase.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/GeomSequence.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/GeomInfo.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/GeomFeature.html",
    "https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/api/com/comsol/model/MeshSequence.html",
    "https://jpype.readthedocs.io/en/latest/userguide.html#array-classes",
)


# A selector's owner matters: Selection.geom(String), for example, is a setter.
# Restricting just the method name would allow a read path to change a model.
SELECTORS: dict[str, tuple[str, ...]] = {
    **{name: ("AbstractModel",) for name in (
        "common", "coordSystem", "cpl", "extraDim", "geom", "material",
        "mesh", "multiphysics", "pair", "physics", "probe", "variable", "view",
    )},
    **{name: ("Model",) for name in ("component", "study", "sol", "result")},
    "param": ("Model", "Results"),
    "geom": ("AbstractModel", "GeomFeature"),
    "func": ("AbstractModel", "MaterialModel", "GeomSequence"),
    "feature": ("ModelEntity",),
    "selection": ("AbstractModel", "SelectionEntity", "PropFeature", "GeomSequence"),
    "propertyGroup": ("Material",),
    "field": ("Physics",),
    "prop": ("Physics",),
    **{name: ("Results",) for name in ("dataset", "numerical", "table", "export")},
    "group": ("ModelParam",),
    "inputParam": ("GeomSequence",),
    "localParam": ("GeomSequence",),
    "obj": ("GeomSequence",),
    **{name: ("MeshSequence",) for name in ("info", "infoCurrent")},
}

# These accessors return one object directly rather than an entity collection.
SELECTOR_COUNTS = {name: {0, 1} for name in SELECTORS}
SELECTOR_COUNTS.update({name: {0} for name in ("inputParam", "localParam", "info", "infoCurrent")})
SELECTOR_COUNTS["obj"] = {1}

READ_METHODS: dict[str, set[int]] = {
    **{name: {0} for name in (
        "tags", "properties", "getType", "label", "tag", "comments", "isActive",
        "hasSelection", "size", "varnames", "dimension", "dim", "named",
        "isGeom", "isGlobal", "isInheriting", "inputDimension", "inputEntities", "objects", "geom",
        "getNBoundaries", "getNDomains", "getNEdges", "getNFaces", "getNVertices",
        "getNEntities", "getBoundingBox", "getSDim", "exists", "isAssembly",
        "lengthUnit", "angularUnit", "geomRep", "isAxisymmetric", "current",
        "objectNames", "problems",
        "autoMeshSize", "isAutomatic", "isComplete", "isEmpty", "autoBuildNew", "autoRebuild",
    )},
    **{name: {1} for name in (
        "get", "descr", "getValueType", "getAllowedPropertyValues", "hasProperty",
        "getBooleanArray", "getBooleanMatrix", "getDoubleArray", "getDoubleMatrix",
        "getIntArray", "getIntMatrix", "getStringArray", "getStringMatrix",
        "getEntryKeys", "interiorEntities", "evaluateUnit",
    )},
    **{name: {1, 2} for name in ("getBoolean", "getDouble", "getInt")},
    "getString": {1, 2, 3},
    "getEntryKeyIndex": {2},
    "entities": {0, 1, 2},
    "evaluate": {1, 2},
    "evaluateComplex": {1, 2},
    "getAdj": {2, 3},
    "getAdjExt": {2, 3},
}

WRITE_METHODS: dict[str, set[int]] = {
    "set": {1, 2, 3}, "setIndex": {3, 4}, "setEntry": {3},
    "create": {1, 2, 3, 4}, "remove": {1, 2}, "move": {2, 3, 4},
    "label": {1}, "comments": {1}, "active": {1}, "descr": {2},
    "named": {1}, "add": {1, 2}, "clear": {0, 1},
    "all": {0, 1}, "allGeom": {0}, "geom": {1, 2, 4}, "init": {0, 1},
    "inherit": {1}, "copy": {2}, "duplicate": {2},
    "rename": {2},
    **{name: {1} for name in (
        "lengthUnit", "angularUnit", "geomRep", "axisymmetric",
        "autoMeshSize", "automatic", "autoBuildNew", "autoRebuild",
    )},
}

SCALAR_TYPES = {"string", "int", "long", "double", "boolean"}


def capability_description(*, write: bool = False) -> str:
    """Describe the same method sets and argument limits that validation uses."""
    methods = WRITE_METHODS if write else READ_METHODS

    def listing(table: dict[str, set[int]]) -> str:
        return "; ".join(f"{name}({','.join(map(str, sorted(counts)))})"
                         for name, counts in sorted(table.items()))

    return "\n".join([
        "Supported selectors (argument counts): " + listing(SELECTOR_COUNTS) + ".",
        "Supported " + ("write" if write else "read") + " methods (argument counts): " + listing(methods) + ".",
        "Counts describe this MCP interface. The selected COMSOL object must implement the requested overload.",
        "Arguments: JSON string/int/double/boolean scalars, or {type, value}. Explicit scalar types: "
        + ", ".join(sorted(SCALAR_TYPES)) + "; append [] or [][] for arrays/matrices, including empty arrays.",
        "Use the COMSOL API reference matching the installed version for concrete property names and overload types.",
    ])


def _scalar(kind: str, value: Any) -> Any:
    if kind == "string" and isinstance(value, str):
        return value
    if kind == "boolean" and isinstance(value, bool):
        return value
    if kind in {"int", "long"} and isinstance(value, int) and not isinstance(value, bool):
        bits = 32 if kind == "int" else 64
        if -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
            return value
    if kind == "double" and isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value):
            return float(value)
    raise ValueError(f"Expected a {kind} argument, received {value!r}.")


def _argument_spec(argument: Any) -> tuple[str, int, Any]:
    if isinstance(argument, dict):
        if set(argument) != {"type", "value"} or not isinstance(argument["type"], str):
            raise ValueError("Typed arguments use exactly {type: 'int[]', value: [1, 2]}.")
        datatype = argument["type"]
        dimensions = 0
        while datatype.endswith("[]"):
            datatype = datatype[:-2]
            dimensions += 1
        if datatype not in SCALAR_TYPES or dimensions > 2:
            raise ValueError("Argument types are string/int/long/double/boolean, with [] or [][] for arrays.")

        def validate(value: Any, depth: int) -> Any:
            if depth == 0:
                return _scalar(datatype, value)
            if not isinstance(value, list):
                raise ValueError("Array arguments require JSON arrays at each array dimension.")
            return [validate(item, depth - 1) for item in value]

        return datatype, dimensions, validate(argument["value"], dimensions)
    if isinstance(argument, bool):
        return "boolean", 0, argument
    if isinstance(argument, str):
        return "string", 0, argument
    if isinstance(argument, int):
        return "int", 0, _scalar("int", argument)
    if isinstance(argument, float):
        return "double", 0, _scalar("double", argument)
    raise ValueError("Use JSON scalars or explicit typed arrays for API arguments.")


def java_argument(argument: Any) -> Any:
    """Make overload selection explicit, including empty and nested arrays."""
    import jpype

    kind, dimensions, value = _argument_spec(argument)
    scalar = {"string": jpype.JString, "int": jpype.JInt, "long": jpype.JLong,
              "double": jpype.JDouble, "boolean": jpype.JBoolean}[kind]
    if dimensions:
        return jpype.JArray(scalar, dimensions)(value)
    return scalar(value)


def validate_steps(steps: list[dict]) -> None:
    if not isinstance(steps, list):
        raise ValueError("steps must be a list of {method, args} selectors; [] selects the model root.")
    for step in steps:
        if not isinstance(step, dict) or set(step) - {"method", "args"}:
            raise ValueError("Each selector has method and optional args only.")
        method = step.get("method")
        arguments = step.get("args", [])
        if not isinstance(method, str) or method not in SELECTORS:
            raise ValueError(f"Unsupported selector {method!r}. Supported selectors: {', '.join(sorted(SELECTORS))}.")
        if not isinstance(arguments, list) or len(arguments) not in SELECTOR_COUNTS[method] or any(
            not isinstance(arg, str) or not arg for arg in arguments
        ):
            raise ValueError(f"Selector {method} accepts argument counts {sorted(SELECTOR_COUNTS[method])}, using exact string tags.")


def validate_call(method: str, args: list, *, write: bool = False) -> None:
    methods = WRITE_METHODS if write else READ_METHODS
    if not isinstance(method, str) or method not in methods:
        raise ValueError(f"Unsupported {'write' if write else 'read'} method {method!r}. Supported: {', '.join(sorted(methods))}.")
    if not isinstance(args, list) or len(args) not in methods[method]:
        raise ValueError(f"{method} accepts argument counts {sorted(methods[method])} in this MCP interface.")
    for argument in args:
        _argument_spec(argument)


def resolve_target(model: Any, steps: list[dict]) -> Any:
    import jpype

    target = model.java
    for index, step in enumerate(steps):
        method = step["method"]
        owners = SELECTORS[method]
        if not any(isinstance(target, jpype.JClass(f"com.comsol.model.{owner}")) for owner in owners):
            raise ValueError(f"Selector {index} ({method}) requires an object implementing {', '.join(owners)}.")
        target = getattr(target, method)(*[jpype.JString(arg) for arg in step.get("args", [])])
        if target is None:
            raise LookupError(f"Selector {index} ({method}) returned no object.")
    return target


def _json_value(value: Any) -> Any:
    """Preserve primitive values and arrays; reject opaque Java object results."""
    import jpype

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (jpype.JBoolean, jpype.JClass("java.lang.Boolean"))):
        return bool(value)
    if isinstance(value, (int, jpype.JClass("java.lang.Integer"), jpype.JClass("java.lang.Long"))):
        return int(value)
    if isinstance(value, (float, jpype.JClass("java.lang.Double"), jpype.JClass("java.lang.Float"))):
        number = float(value)
        return number if math.isfinite(number) else {"nonfinite_number": str(number)}
    if isinstance(value, jpype.JString):
        return str(value)
    if isinstance(value, (list, tuple, jpype.JArray)):
        return [_json_value(item) for item in value]
    raise TypeError(f"Read returned an object of type {type(value).__name__}; select a primitive or array getter.")


def read_value(model: Any, steps: list[dict], method: str, args: list) -> Any:
    target = resolve_target(model, steps)
    return _json_value(getattr(target, method)(*[java_argument(arg) for arg in args]))


def validate_reads(reads: list[dict]) -> None:
    if not isinstance(reads, list):
        raise ValueError("before must be a list of explicit reads.")
    for read in reads:
        if not isinstance(read, dict) or set(read) - {"steps", "method", "args"}:
            raise ValueError("Each evidence read contains steps, method, and optional args.")
        validate_steps(read.get("steps"))
        validate_call(read.get("method"), read.get("args", []))


def validate_checks(checks: list[dict], *, required: bool) -> None:
    if not isinstance(checks, list) or (required and not checks):
        raise ValueError("verify must be a nonempty list of reads and expected results.")
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"steps", "method", "args", "expect"}:
            raise ValueError("Each check contains steps, method, optional args, and expect.")
        validate_steps(check.get("steps"))
        validate_call(check.get("method"), check.get("args", []))
        expected = check.get("expect")
        if not isinstance(expected, dict) or set(expected) != {"operator", "value"}:
            raise ValueError("expect contains operator and value, e.g. {operator: 'equals', value: '5[mm]' }.")
        if expected["operator"] not in {"equals", "contains", "not_contains", "same_items"}:
            raise ValueError("Expected operator: equals, contains, not_contains, or same_items.")
        if expected["operator"] == "same_items" and not isinstance(expected["value"], list):
            raise ValueError("same_items requires an expected array.")
        json.dumps(expected["value"], allow_nan=False)


def _equal(actual: Any, expected: Any) -> bool:
    # Keep JSON booleans distinct from numbers (Python considers True == 1).
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(_equal(a, e) for a, e in zip(actual, expected))
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_equal(actual[k], expected[k]) for k in actual)
    return actual == expected


def matches(actual: Any, expect: dict) -> bool:
    operator, expected = expect["operator"], expect["value"]
    if operator == "equals":
        return _equal(actual, expected)
    if not isinstance(actual, list):
        raise ValueError(f"{operator} requires an array result.")
    if operator in {"contains", "not_contains"}:
        present = any(_equal(item, expected) for item in actual)
        return present if operator == "contains" else not present
    if not isinstance(expected, list):
        raise ValueError("same_items requires an expected array.")
    remaining = list(actual)
    for value in expected:
        for index, item in enumerate(remaining):
            if _equal(item, value):
                remaining.pop(index)
                break
        else:
            return False
    return not remaining


def read_checks(model: Any, checks: list[dict]) -> list[dict]:
    results = []
    for check in checks:
        result = {"request": check, "read_success": False, "passed": False}
        try:
            result["actual"] = read_value(model, check["steps"], check["method"], check.get("args", []))
            result["read_success"] = True
            result["passed"] = matches(result["actual"], check["expect"])
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        results.append(result)
    return results


def read_evidence(model: Any, reads: list[dict]) -> list[dict]:
    """Collect independent before-write facts without treating them as conditions."""
    results = []
    for read in reads:
        result = {"request": read, "read_success": False}
        try:
            result["actual"] = read_value(model, read["steps"], read["method"], read.get("args", []))
            result["read_success"] = True
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        results.append(result)
    return results


def record_failure(packet: dict, exc: Exception) -> None:
    error = f"{type(exc).__name__}: {exc}"
    packet["errors"].append(error)
    if "stage" in packet:
        packet.setdefault("failure_details", []).append({"stage": packet["stage"], "error": error})
    if hasattr(exc, "candidate_tags"):
        packet["candidate_tags"] = exc.candidate_tags
    if hasattr(exc, "to_result"):
        packet["failure"] = exc.to_result()


def identify_record(packet: dict, record: Any) -> None:
    if record is None:
        raise LookupError("Discover and attach the intended model before executing an API task.")
    packet["model_tag"] = record.tag
    packet["model"] = {
        "tag": record.tag, "name": record.name, "comsol_version": record.comsol_version,
        "file_path": record.file_path, "origin": record.origin,
    }
