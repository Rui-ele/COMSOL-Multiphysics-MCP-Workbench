"""MCP entry points for explicit COMSOL Java reads and verified model changes."""

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ..core import model_api
from ..core.reports import finish_api_packet, new_report_packet
from ..core.session import session_manager


class ApiStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(description="COMSOL object accessor, e.g. component, physics, feature, material, propertyGroup, selection.")
    args: list[str] = Field(default_factory=list, description="[] for a collection, or one exact COMSOL tag such as ['comp1'].")


class ApiExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator: Literal["equals", "contains", "not_contains", "same_items"]
    value: Any = Field(description="Expected JSON value; same_items compares arrays without considering order.")


class ApiRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[ApiStep] = Field(description="Full object path starting from the model root.")
    method: str = Field(description="Read method such as getString, getDoubleArray, entities, or tags.")
    args: list[Any] = Field(default_factory=list, description="Read arguments; arrays use {type: 'int[]', value: [1, 2]}.")


class ApiCheck(ApiRead):
    expect: ApiExpectation


READ_DESCRIPTION = """Read one planned COMSOL Java getter and return complete facts.

steps is the exact accessor chain from the model root, for example
[{"method":"component","args":["comp1"]},{"method":"physics","args":["ht"]},
{"method":"feature","args":["hf1"]}]. Use [] for the model root. Accessors
normally take no arguments for a collection or one exact tag for an object.
Get a collection's actual child tags with method="tags", args=[].
For a feature, properties lists names and getString with args=["q0"] reads
the stored expression. Material settings use material/propertyGroup;
global parameters use steps=[{"method":"param"}], method="get", args=["L"].
Parameter evaluate/evaluateUnit reads numerical values/units without solving.
Geometry getNBoundaries/getNDomains/getBoundingBox reads the current geometry.

Reports retain requested calls, actual values and exact errors. Large reports
return report_delivery; retrieve the frozen remainder with
diagnostic_report_page(report_id, offset=next_offset).
""" + model_api.capability_description()


WRITE_DESCRIPTION = """Execute one planned COMSOL operation and verify actual readback.

steps, method and typed args identify one call, using the same object paths as
comsol_api_read. Create, configure and remove nodes through their actual parent
collections. The COMSOL version's API defines properties and overloads.

preconditions is an optional list of required original-state checks. A failed
read or comparison prevents the operation. before is optional evidence reads
with {steps, method, args}, without expect; failures are retained in the report
and execution continues. verify is a required nonempty list of post-operation
checks with {steps, method, args, expect:{operator, value}}. Each read has its
own complete path. The operators are equals, contains, not_contains, same_items.
same_items compares arrays ignoring order. Use a parent's tags() with contains
or not_contains to verify create/remove; new nodes are read only after creation.

The tool calls the operation once, then performs every verification read even
if the operation raised an error. verified requires the operation to return
and every verification to pass. Rejected means no write was attempted;
needs_review retains attempted effects, readback and exact errors. A before
read failure remains visible even when the operation itself is verified.
Large frozen reports continue through diagnostic_report_page.
""" + model_api.capability_description(write=True)


def register_model_api_tools(mcp: FastMCP) -> None:
    @mcp.tool(description=READ_DESCRIPTION,
              annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def comsol_api_read(
        model_name: str,
        steps: list[ApiStep],
        method: str,
        args: list[Any] | None = None,
    ) -> dict:
        """Read one planned COMSOL getter."""
        selectors = [step.model_dump() for step in steps]
        arguments = {"model_name": model_name, "steps": selectors, "method": method,
                     "args": [] if args is None else args}
        packet = new_report_packet("comsol_api_read", arguments)
        try:
            if not isinstance(model_name, str) or not model_name.strip():
                raise ValueError("model_name must be an exact tag or unique registered name.")
            model_api.validate_steps(selectors)
            model_api.validate_call(method, arguments["args"])
            record = session_manager.get_model_record(model_name)
            model_api.identify_record(packet, record)
            packet["value"] = model_api.read_value(record.model, selectors, method, arguments["args"])
            packet.update(success=True, status="read")
        except Exception as exc:
            packet["status"] = "error"
            model_api.record_failure(packet, exc)
        return finish_api_packet(packet)

    @mcp.tool(description=WRITE_DESCRIPTION,
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
    def comsol_api_write(
        model_name: str,
        steps: list[ApiStep],
        method: str,
        args: list[Any],
        verify: list[ApiCheck],
        preconditions: list[ApiCheck] | None = None,
        before: list[ApiRead] | None = None,
    ) -> dict:
        """Check required state, collect requested evidence, write once, then verify."""
        selectors = [step.model_dump() for step in steps]
        verification_reads = [check.model_dump() for check in verify]
        conditions = [check.model_dump() for check in preconditions] if preconditions is not None else []
        before_reads = [read.model_dump() for read in before] if before is not None else []
        arguments = {"model_name": model_name, "steps": selectors, "method": method,
                     "args": args, "verify": verification_reads, "preconditions": conditions,
                     "before": before_reads}
        packet = new_report_packet("comsol_api_write", arguments)
        packet.update(write_attempted=False, write_returned=False, preconditions=[],
                      before=[], after=[], verification=[], stage="validation")
        try:
            if not isinstance(model_name, str) or not model_name.strip():
                raise ValueError("model_name must be an exact tag or unique registered name.")
            model_api.validate_steps(selectors)
            model_api.validate_call(method, args, write=True)
            model_api.validate_checks(verification_reads, required=True)
            model_api.validate_checks(arguments["preconditions"], required=False)
            model_api.validate_reads(before_reads)
            packet["stage"] = "model_resolution"
            record = session_manager.get_model_record(model_name)
            model_api.identify_record(packet, record)
            packet["stage"] = "preconditions"
            packet["preconditions"] = model_api.read_checks(record.model, conditions)
            if not all(check["read_success"] and check["passed"] for check in packet["preconditions"]):
                raise ValueError("One or more original-state conditions failed; review the precondition readings.")
            packet["stage"] = "before"
            packet["before"] = model_api.read_evidence(record.model, before_reads)
            packet["before_complete"] = all(read["read_success"] for read in packet["before"])
            packet["stage"] = "operation_preparation"
            target = model_api.resolve_target(record.model, selectors)
            # Prepare argument conversions and method lookup before any write.
            java_args = [model_api.java_argument(arg) for arg in args]
            operation = getattr(target, method)
            packet["stage"] = "write"
            packet["write_attempted"] = True
            try:
                operation(*java_args)
                packet["write_returned"] = True
            except Exception as exc:
                model_api.record_failure(packet, exc)
            packet["stage"] = "verification"
            packet["after"] = model_api.read_checks(record.model, verification_reads)
            packet["verification"] = [
                {"index": index, "passed": check["read_success"] and check["passed"]}
                for index, check in enumerate(packet["after"])
            ]
            packet["success"] = packet["write_returned"] and all(
                check["passed"] for check in packet["verification"]
            )
            packet["status"] = "verified" if packet["success"] else "needs_review"
            packet["stage"] = "complete"
        except Exception as exc:
            packet["status"] = "needs_review" if packet["write_attempted"] else "rejected"
            model_api.record_failure(packet, exc)
        return finish_api_packet(packet)
