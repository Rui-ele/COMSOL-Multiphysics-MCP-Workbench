"""Build geometry and import a file through explicitly identified nodes."""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..core.session import session_manager
from ..core.reports import read


def _geometry(model_name: str, component_tag: str, geometry_tag: str):
    record = session_manager.get_model_record(model_name)
    if record is None or record.tag != model_name:
        raise ValueError(f"Registered model tag not found: {model_name}")
    java = record.model.java
    if component_tag not in [str(tag) for tag in java.component().tags()]:
        raise ValueError(f"Component tag not found: {component_tag}")
    component = java.component(component_tag)
    if geometry_tag not in [str(tag) for tag in component.geom().tags()]:
        raise ValueError(f"Geometry tag not found in {component_tag}: {geometry_tag}")
    return component.geom(geometry_tag)


def register_geometry_tools(mcp: FastMCP) -> None:
    """Register execution mechanisms; node configuration uses comsol_api_write."""

    @mcp.tool()
    def geometry_build(model_name: str, component_tag: str, geometry_tag: str) -> dict:
        """Build one existing geometry sequence via its Java run() method.

        All identifiers are actual tags. COMSOL builds the features configured
        in that sequence. Returns the exact call, completion, and independently
        read current feature, object names, and problem tags. Configure geometry
        through comsol_api_write and call this tool when the task requires a build.
        """
        source = f"model({model_name}).component({component_tag}).geom({geometry_tag})"
        result = {"success": False, "model_tag": model_name,
                  "component_tag": component_tag, "geometry_tag": geometry_tag,
                  "node": source, "method": "run", "args": [],
                  "write_attempted": False, "execution_completed": False}
        geometry = None
        try:
            geometry = _geometry(model_name, component_tag, geometry_tag)
            result["write_attempted"] = True
            geometry.run()
            result.update(success=True, execution_completed=True)
        except Exception as exc:
            result.update(error=str(exc), error_type=type(exc).__name__)
        if geometry is not None and result["write_attempted"]:
            result["readback"] = {
                "current_feature": read(source + ".current()", lambda: geometry.current()),
                "object_names": read(source + ".objectNames()", lambda: geometry.objectNames()),
                "problem_tags": read(source + ".problems()", lambda: geometry.problems()),
            }
        return result

    @mcp.tool()
    def geometry_import(
        model_name: str, component_tag: str, geometry_tag: str,
        import_tag: str, file_path: str,
    ) -> dict:
        """Import an absolute CAD file into one existing geometry Import node.

        The file must be visible at the same absolute path to MCP and COMSOL.
        Sets filename, calls importData(), and reads filename back. The returned
        stages distinguish filename changes from completed import. Other import
        settings come from the node; geometry_build runs the configured geometry
        separately when requested. Create/configure the node with comsol_api_write.
        """
        source = (f"model({model_name}).component({component_tag})"
                  f".geom({geometry_tag}).feature({import_tag})")
        result = {"success": False, "model_tag": model_name, "node": source,
                  "component_tag": component_tag, "geometry_tag": geometry_tag,
                  "import_tag": import_tag, "file_path": file_path,
                  "write_attempted": False, "execution_completed": False,
                  "completed_actions": []}
        node = None
        try:
            path = Path(file_path).expanduser()
            if not path.is_absolute():
                raise ValueError("file_path must be an absolute path.")
            if not path.is_file():
                raise ValueError(f"Import file not found on the MCP host: {path}")
            geometry = _geometry(model_name, component_tag, geometry_tag)
            if import_tag not in [str(tag) for tag in geometry.feature().tags()]:
                raise ValueError(f"Import node tag not found: {import_tag}")
            node = geometry.feature(import_tag)
            if str(node.getType()) != "Import":
                raise ValueError(f"Node {import_tag} has type {node.getType()}, expected Import.")
            result["before"] = read(source + ".getString(filename)", lambda: node.getString("filename"))
            result["file_path"] = str(path)
            result["method"] = "set"
            result["args"] = ["filename", str(path)]
            result["write_attempted"] = True
            node.set("filename", str(path))
            result["completed_actions"].append({"method": "set", "args": ["filename", str(path)]})
            actual = str(node.getString("filename"))
            result["readback"] = {"filename": actual}
            result["filename_verified"] = actual == str(path)
            if not result["filename_verified"]:
                raise ValueError("The import filename readback differs from the requested path.")
            result["method"] = "importData"
            result["args"] = []
            node.importData()
            result["completed_actions"].append({"method": "importData", "args": []})
            result["execution_completed"] = True
            result["readback"]["object_names"] = read(source + ".objectNames()", lambda: node.objectNames())
            result["readback"]["status"] = read(source + ".status()", lambda: node.status())
            result["success"] = result["filename_verified"]
        except Exception as exc:
            result.update(error=str(exc), error_type=type(exc).__name__)
            if node is not None and result["write_attempted"]:
                result["readback"] = {
                    "filename": read(source + ".getString(filename)", lambda: node.getString("filename"))
                }
        return result
