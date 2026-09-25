"""Execute one explicitly identified mesh sequence."""

from mcp.server.fastmcp import FastMCP

from ..core.session import session_manager
from ..core.reports import read


def register_mesh_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def mesh_build(model_name: str, component_tag: str, mesh_tag: str) -> dict:
        """Run one existing mesh sequence and read its completion state.

        Supply actual model/component/mesh tags. Calls component.mesh(mesh_tag)
        .run() with the node's existing settings. Mesh node creation and property
        changes use comsol_api_write. The response preserves run failures and
        independently reports isComplete(), current feature, and problem tags.
        """
        source = f"model({model_name}).component({component_tag}).mesh({mesh_tag})"
        result = {"success": False, "model_tag": model_name, "node": source,
                  "component_tag": component_tag, "mesh_tag": mesh_tag,
                  "method": "run", "args": [], "write_attempted": False,
                  "execution_completed": False}
        mesh = None
        try:
            record = session_manager.get_model_record(model_name)
            if record is None or record.tag != model_name:
                raise ValueError(f"Registered model tag not found: {model_name}")
            java = record.model.java
            if component_tag not in [str(tag) for tag in java.component().tags()]:
                raise ValueError(f"Component tag not found: {component_tag}")
            component = java.component(component_tag)
            if mesh_tag not in [str(tag) for tag in component.mesh().tags()]:
                raise ValueError(f"Mesh tag not found in {component_tag}: {mesh_tag}")
            mesh = component.mesh(mesh_tag)
            result["write_attempted"] = True
            mesh.run()
            result.update(success=True, execution_completed=True)
        except Exception as exc:
            result.update(error=str(exc), error_type=type(exc).__name__)
        if mesh is not None and result["write_attempted"]:
            result["readback"] = {
                "is_complete": read(source + ".isComplete()", lambda: bool(mesh.isComplete())),
                "current_feature": read(source + ".current()", lambda: mesh.current()),
                "problem_tags": read(source + ".problems()", lambda: mesh.problems()),
            }
            completion = result["readback"]["is_complete"]
            if result["execution_completed"] and completion.get("value") is False:
                result.update(success=False, error="COMSOL returned from run() but reports an incomplete mesh.")
        return result
