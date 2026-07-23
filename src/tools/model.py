"""Model management tools for COMSOL MCP Server."""

from typing import Optional
from pathlib import Path
from mcp.server.fastmcp import FastMCP
import mph

from .session import AmbiguousModelError, session_manager
from ..utils.versioning import (
    generate_version_path, 
    generate_latest_path,
    parse_version_info,
    list_model_versions,
    get_model_directory,
    MODELS_BASE_DIR
)


def register_model_tools(mcp: FastMCP) -> None:
    """Register model management tools with the MCP server."""

    @mcp.tool()
    def model_discover() -> dict:
        """
        Discover every model currently held by the connected COMSOL Server.

        Discovery is read-only. It reports exact COMSOL model tags, display
        names, saved file paths when available, and whether each model is
        already registered with MCP. It does not attach models or change the
        current model.
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session."}
        try:
            return session_manager.discover_server_models()
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to discover server models: {exc}",
            }

    @mcp.tool()
    def model_attach(model_tag: str, set_current: bool = True) -> dict:
        """
        Attach an existing COMSOL Server model to MCP by exact tag.

        Newly attached models are registered as external and observe-only.
        This tool does not load, copy, save, or modify the server model.
        Args:
            model_tag: Exact COMSOL model tag returned by model_discover.
            set_current: Whether to make the attached model current.
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session."}
        if not model_tag:
            return {"success": False, "error": "model_tag must not be empty."}
        try:
            record, newly_attached = session_manager.attach_server_model(
                model_tag,
                set_current=set_current,
            )
            return {
                "success": True,
                "model_tag": record.tag,
                "model": record.metadata(
                    is_current=record.tag == session_manager.current_model_tag
                ),
                "attached": newly_attached,
                "already_registered": not newly_attached,
                "message": (
                    "Server model attached in observe mode. "
                    "Model-changing tools are blocked until write access is enabled."
                    if newly_attached
                    else "Server model was already registered; metadata was preserved."
                ),
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to attach server model: {exc}",
                "model_tag": model_tag,
            }

    @mcp.tool()
    def model_detach(model_name: str) -> dict:
        """
        Detach an external model from MCP without removing it from Server.

        Args:
            model_name: Exact COMSOL tag or unique display name.
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session."}
        try:
            record = session_manager.unregister_external_model(model_name)
        except AmbiguousModelError as exc:
            return {
                "success": False,
                "error": str(exc),
                "candidate_tags": exc.candidate_tags,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if record is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name}",
            }
        return {
            "success": True,
            "detached": record.name,
            "detached_tag": record.tag,
            "server_model_preserved": True,
            "current_model": session_manager.current_model,
            "current_model_tag": session_manager.current_model_tag,
        }

    @mcp.tool()
    def model_access_set(model_name: str, access_mode: str) -> dict:
        """
        Change MCP access mode for a registered model.

        This only changes MCP registry metadata. It does not modify, save, or
        lock the COMSOL model. Use ``write`` deliberately before model-changing
        calls, then return externally attached models to ``observe`` afterward.

        Args:
            model_name: Exact COMSOL tag or unique display name.
            access_mode: Either ``observe`` or ``write``.
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session."}
        try:
            changed = session_manager.set_model_access(model_name, access_mode)
        except AmbiguousModelError as exc:
            return {
                "success": False,
                "error": str(exc),
                "candidate_tags": exc.candidate_tags,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        if changed is None:
            return {"success": False, "error": f"Model not found: {model_name}"}

        record, previous_mode = changed
        was_changed = previous_mode != record.access_mode
        if record.access_mode == "write":
            hint = (
                "Write access is enabled. MCP model-changing tools may now "
                "modify this server model; return it to observe mode afterward."
            )
        else:
            hint = (
                "Observe mode is enabled. Reads and reports remain available, "
                "while model-changing tools and exports are blocked."
            )
        return {
            "success": True,
            "model": record.name,
            "model_tag": record.tag,
            "previous_access_mode": previous_mode,
            "access_mode": record.access_mode,
            "changed": was_changed,
            "hint": hint,
        }
    
    @mcp.tool()
    def model_load(file_path: str, set_current: bool = True) -> dict:
        """
        Load a COMSOL model from a .mph file.
        
        Args:
            file_path: Absolute or relative path to the .mph model file
            set_current: Whether to set this as the current active model (default: True)
        
        Returns:
            Model info including name, file path, and version, or error message
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session. Start with comsol_start first."}
        
        client = session_manager.client
        if client is None:
            return {"success": False, "error": "Client not available."}
        
        try:
            path = Path(file_path)
            if not path.exists():
                return {"success": False, "error": f"File not found: {file_path}"}
            if not path.suffix.lower() == ".mph":
                return {"success": False, "error": f"File must be a .mph file: {file_path}"}
            
            model = client.load(str(path.absolute()))
            record = session_manager.add_model(model, origin="mcp_loaded")
            name = record.name
            
            if set_current:
                session_manager.set_current_model(record.tag)
            
            version_info = parse_version_info(name)
            
            return {
                "success": True,
                "model_tag": record.tag,
                "model": {
                    "name": name,
                    "tag": record.tag,
                    "file": str(path.absolute()),
                    "comsol_version": model.version(),
                    "is_versioned": version_info is not None,
                    "version_info": version_info,
                },
                "desktop_import": session_manager.desktop_import_hint(name),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to load model: {str(e)}"}
    
    @mcp.tool()
    def model_create(name: Optional[str] = None, set_current: bool = True) -> dict:
        """
        Create a new empty COMSOL model.
        
        Args:
            name: Optional name for the model (auto-generated if not provided)
            set_current: Whether to set this as the current active model (default: True)
        
        Returns:
            Model info including name, or error message
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session. Start with comsol_start first."}
        
        client = session_manager.client
        if client is None:
            return {"success": False, "error": "Client not available."}
        
        try:
            model = client.create(name)
            record = session_manager.add_model(model, origin="mcp_created")
            model_name = record.name
            
            if set_current:
                session_manager.set_current_model(record.tag)
            
            return {
                "success": True,
                "model_tag": record.tag,
                "model": {
                    "name": model_name,
                    "tag": record.tag,
                    "is_new": True,
                },
                "desktop_import": session_manager.desktop_import_hint(model_name),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create model: {str(e)}"}
    
    @mcp.tool()
    def model_create_component(
        component_name: str = "comp1",
        space_dimension: int = 3,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Create a component in the model (required before adding geometry/physics).

        Components are containers for geometry, physics, materials, and mesh.
        Must be created before adding geometry or physics.

        Args:
            component_name: Name for the component (default: 'comp1')
            space_dimension: Space dimension - 0=0D, 1=1D, 2=2D, 3=3D, 20=2D axisymmetric, 30=3D axisymmetric (default: 3)
            model_name: Unique model name or exact COMSOL tag (default: current)

        Returns:
            Created component info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            comp = jm.component().create(component_name, True, space_dimension)

            return {
                "success": True,
                "component": component_name,
                "space_dimension": space_dimension,
                "model": model.name(),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create component: {str(e)}"}
    
    @mcp.tool()
    def model_list_components(
        model_name: Optional[str] = None
    ) -> dict:
        """
        List all components in a model.
        
        Args:
            model_name: Unique model name or exact COMSOL tag (default: current)
        
        Returns:
            List of component names
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            jm = model.java
            components = []
            
            for i in range(jm.component().size()):
                comp = jm.component().get(i)
                if comp is not None:
                    components.append({
                        "name": comp.tag(),
                        "label": comp.label() if hasattr(comp, 'label') else comp.tag()
                    })
            
            return {
                "success": True,
                "components": components,
                "count": len(components),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list components: {str(e)}"}
    
    @mcp.tool()
    def model_save(
        model_name: Optional[str] = None,
        file_path: Optional[str] = None,
        format: Optional[str] = None
    ) -> dict:
        """
        Save a COMSOL model to file.
        
        Args:
            model_name: Unique model name or exact COMSOL tag (default: current)
            file_path: Path to save to (default: original file path)
            format: Save format - 'Comsol', 'Java', 'Matlab', or 'VBA' (default: Comsol/.mph)
        
        Returns:
            Save confirmation with file path, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            model.save(path=file_path, format=format)
            saved_path = file_path or model.file()
            
            return {
                "success": True,
                "model": model.name(),
                "saved_to": str(saved_path),
                "format": format or "Comsol",
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to save model: {str(e)}"}
    
    @mcp.tool()
    def model_save_version(
        model_name: Optional[str] = None,
        description: Optional[str] = None
    ) -> dict:
        """
        Save a model with a timestamp version suffix.
        
        Creates a new file with structured path: 
        ./comsol_models/{model_name}/{model_name}_{timestamp}.mph
        
        Also saves a 'latest' copy: 
        ./comsol_models/{model_name}/{model_name}_latest.mph
        
        Useful for version control and design iterations.
        
        Args:
            model_name: Unique model name or exact COMSOL tag (default: current)
            description: Optional description for this version (stored in metadata)
        
        Returns:
            Save confirmation with versioned file path, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            # Get model name for directory structure
            name = model.name()
            
            # Generate versioned path using new structure
            versioned_path = generate_version_path(name)
            
            # Save versioned copy
            model.save(path=versioned_path)
            
            # Also save as 'latest'
            latest_path = generate_latest_path(name)
            model.save(path=latest_path)
            
            return {
                "success": True,
                "model": name,
                "version_path": versioned_path,
                "latest_path": latest_path,
                "description": description,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to save version: {str(e)}"}
    
    @mcp.tool()
    def model_list() -> dict:
        """
        List all models currently loaded in the COMSOL session.
        
        Returns:
            List of models with their names, file paths, and status
        """
        if not session_manager.is_connected:
            return {"success": False, "error": "No active COMSOL session."}
        
        registry_sync_error = None
        try:
            session_manager.synchronize_model_registry()
        except Exception as exc:
            registry_sync_error = str(exc)
        observed_tags, observer_error = session_manager._observer_state()

        records = (
            session_manager.model_records
            if registry_sync_error is None
            else session_manager._models.copy()
        )
        current_record = (
            records.get(session_manager.current_model_tag)
            if session_manager.current_model_tag
            else None
        )
        current = current_record.name if current_record else None
        current_tag = session_manager.current_model_tag
        
        model_list = []
        for tag, record in records.items():
            model = record.model
            info = record.metadata(is_current=tag == current_tag)
            info["used_by_other_clients"] = (
                tag in observed_tags if observer_error is None else None
            )
            info["desktop_attached"] = info["used_by_other_clients"]
            if record.stale:
                info["file"] = record.file_path
                info["comsol_version"] = record.comsol_version
            else:
                try:
                    info["file"] = model.file()
                    info["comsol_version"] = model.version()
                except Exception:
                    info["file"] = record.file_path
                    info["comsol_version"] = record.comsol_version
            model_list.append(info)
        
        result = {
            "success": True,
            "models": model_list,
            "count": len(model_list),
            "current_model": current,
            "current_model_tag": current_tag,
            "registry_state_available": registry_sync_error is None,
        }
        if registry_sync_error:
            result["registry_sync_error"] = registry_sync_error
        if observer_error:
            result["observer_check_error"] = observer_error
        return result
    
    @mcp.tool()
    def model_set_current(model_name: str) -> dict:
        """
        Set the current active model for subsequent operations.
        
        Args:
            model_name: Unique model name or exact COMSOL tag
        
        Returns:
            Confirmation or error message
        """
        try:
            record = session_manager.get_model_record(model_name)
        except AmbiguousModelError as exc:
            return {
                "success": False,
                "error": str(exc),
                "candidate_tags": exc.candidate_tags,
            }
        if record is not None and session_manager.set_current_model(record.tag):
            return {
                "success": True,
                "current_model": record.name,
                "current_model_tag": record.tag,
            }
        return {
            "success": False,
            "error": f"Model not found: {model_name}"
        }
    
    @mcp.tool()
    def model_clone(
        model_name: Optional[str] = None,
        new_name: Optional[str] = None,
        set_current: bool = False
    ) -> dict:
        """
        Clone a model to create a copy for comparison or modification.
        
        Args:
            model_name: Unique model name or exact COMSOL tag (default: current)
            new_name: Name for the cloned model (auto-generated if not provided)
            set_current: Whether to set the clone as current model (default: False)
        
        Returns:
            Info about the cloned model, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            client = session_manager.client
            if client is None:
                return {"success": False, "error": "Client not available."}
            
            java_model = model.java.createCopy()
            if new_name:
                java_model.label(new_name)
            
            cloned_model = mph.Model(java_model)
            clone_record = session_manager.add_model(
                cloned_model,
                origin="mcp_cloned",
            )
            clone_name = clone_record.name
            
            if set_current:
                session_manager.set_current_model(clone_record.tag)
            
            return {
                "success": True,
                "model_tag": clone_record.tag,
                "original": model.name(),
                "clone": clone_name,
                "is_current": clone_record.tag == session_manager.current_model_tag,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to clone model: {str(e)}"}
    
    @mcp.tool()
    def model_remove(model_name: str) -> dict:
        """
        Remove a model from memory.
        
        Args:
            model_name: Unique model name or exact COMSOL tag
        
        Returns:
            Confirmation or error message
        """
        try:
            record = session_manager.get_model_record(model_name)
        except AmbiguousModelError as exc:
            return {
                "success": False,
                "error": str(exc),
                "candidate_tags": exc.candidate_tags,
            }

        if record is not None and session_manager.remove_model(record.tag):
            return {
                "success": True,
                "removed": model_name,
                "removed_model": record.name,
                "removed_tag": record.tag,
                "current_model": session_manager.current_model,
                "current_model_tag": session_manager.current_model_tag,
            }
        return {
            "success": False,
            "error": f"Failed to remove model: {model_name}"
        }
    
    @mcp.tool()
    def model_inspect(model_name: Optional[str] = None) -> dict:
        """
        Get detailed information about a model's structure and contents.
        
        Args:
            model_name: Unique model name or exact COMSOL tag (default: current)
        
        Returns:
            Detailed model structure including parameters, physics, studies, etc.
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            info = {
                "name": model.name(),
                "tag": str(model.java.tag()),
                "file": model.file(),
                "comsol_version": model.version(),
                "parameters": dict(model.parameters()) if model.parameters() else {},
                "functions": model.functions(),
                "components": model.components(),
                "geometries": model.geometries(),
                "selections": model.selections(),
                "physics": model.physics(),
                "multiphysics": model.multiphysics(),
                "materials": model.materials(),
                "meshes": model.meshes(),
                "studies": model.studies(),
                "solutions": model.solutions(),
                "datasets": model.datasets(),
                "plots": model.plots(),
                "exports": model.exports(),
                "modules": model.modules(),
            }
            
            problems = model.problems()
            if problems:
                info["problems"] = problems
            
            return {"success": True, "model": info}
        except Exception as e:
            return {"success": False, "error": f"Failed to inspect model: {str(e)}"}
