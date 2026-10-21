"""GLB download/cache/import helpers."""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from .cache import AssetCache
from .config import MintBlenderConfig
from .http import urlopen_with_config

UrlOpen = Callable[..., Any]


class ImportUnsupportedError(RuntimeError):
    pass


SCENE_CHANGED_MESSAGE = "Scene changed since preview, preview again"
MODEL_IMPORT_LOCATION = "File origin (glTF coordinates)"
_CREATED_DATA_KINDS = (
    "objects",
    "meshes",
    "materials",
    "images",
    "textures",
    "node_groups",
    "armatures",
    "actions",
    "cameras",
    "lights",
    "collections",
)


class SceneChangedError(RuntimeError):
    def __init__(self, message: str = SCENE_CHANGED_MESSAGE):
        super().__init__(message)


def _load_bpy() -> Any:
    try:
        import bpy  # type: ignore
    except ImportError as error:
        raise RuntimeError("Blender bpy module is not available") from error
    return bpy


def _read_url(url: str, config: MintBlenderConfig, urlopen: Optional[UrlOpen]) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "model/gltf-binary, application/octet-stream, */*",
            "User-Agent": "MintBlenderAssetBrowser/0.1.0",
        },
        method="GET",
    )
    response = urlopen_with_config(request, config, urlopen, timeout=30)
    return response.read()


def _clean_asset_name(name: Any) -> Optional[str]:
    if not isinstance(name, str):
        return None

    cleaned = " ".join(name.split()).strip()
    return cleaned[:80] if cleaned else None


def _name_part(base: str, suffix: Optional[str], index: int, total: int) -> str:
    label = f"{base} {suffix}" if suffix else base
    if total <= 1:
        return label

    return f"{label} {index + 1:02d}"


def _scene_objects(bpy_ref: Any) -> list[Any]:
    context = getattr(bpy_ref, "context", None)
    scene = getattr(context, "scene", None)
    objects = getattr(scene, "objects", None)
    return list(objects) if objects is not None else []


def _selected_objects(bpy_ref: Any) -> list[Any]:
    context = getattr(bpy_ref, "context", None)
    selected = getattr(context, "selected_objects", None)
    return list(selected) if selected is not None else []


def _unique_by_identity(items: list[Any]) -> list[Any]:
    seen = set()
    unique = []
    for item in items:
        if item is None:
            continue

        item_id = id(item)
        if item_id in seen:
            continue

        seen.add(item_id)
        unique.append(item)

    return unique


def _imported_objects_after(bpy_ref: Any, before_ids: set[int]) -> list[Any]:
    after = _scene_objects(bpy_ref)
    imported = [obj for obj in after if id(obj) not in before_ids]
    return imported or _selected_objects(bpy_ref)


def _materials_for_objects(objects: list[Any]) -> list[Any]:
    materials = []
    for obj in objects:
        for slot in list(getattr(obj, "material_slots", None) or []):
            materials.append(getattr(slot, "material", None))

        data = getattr(obj, "data", None)
        for material in list(getattr(data, "materials", None) or []):
            materials.append(material)

    return _unique_by_identity(materials)


def _rename_named_items(items: list[Any], base: str, suffix: Optional[str]) -> None:
    total = len(items)
    for index, item in enumerate(items):
        if hasattr(item, "name"):
            item.name = _name_part(base, suffix, index, total)


def _rename_imported_asset(imported_objects: list[Any], asset_name: Any) -> None:
    base_name = _clean_asset_name(asset_name)
    objects = _unique_by_identity(imported_objects)
    if not base_name or not objects:
        return

    meshes = _unique_by_identity([
        getattr(obj, "data", None)
        for obj in objects
        if hasattr(getattr(obj, "data", None), "name")
    ])
    materials = _materials_for_objects(objects)

    _rename_named_items(objects, base_name, None)
    _rename_named_items(meshes, base_name, "Mesh")
    _rename_named_items(materials, base_name, "Material")


def _target_collection(bpy_ref: Any) -> Any:
    context = getattr(bpy_ref, "context", None)
    layer_collection = getattr(
        getattr(context, "view_layer", None), "active_layer_collection", None
    )
    collection = getattr(layer_collection, "collection", None)
    if collection is None:
        collection = getattr(getattr(context, "scene", None), "collection", None)
    return collection


def _collection_exists(bpy_ref: Any, collection: Any) -> bool:
    if collection is None:
        return False
    scene = getattr(getattr(bpy_ref, "context", None), "scene", None)
    if collection is getattr(scene, "collection", None):
        return True
    collections = getattr(getattr(bpy_ref, "data", None), "collections", None)
    try:
        return getattr(collection, "name", None) in collections
    except TypeError:
        return False


def import_preview(bpy_ref: Any) -> dict:
    """Describe where an import will land, plus a cheap scene fingerprint."""
    context = getattr(bpy_ref, "context", None)
    scene = getattr(context, "scene", None)
    collection = _target_collection(bpy_ref)
    cursor = getattr(getattr(scene, "cursor", None), "location", None)
    try:
        cursor_values = [round(float(value), 5) for value in cursor]
    except TypeError:
        cursor_values = []
    objects = getattr(collection, "objects", None)
    return {
        "scene": getattr(scene, "name", None),
        "collection": getattr(collection, "name", None),
        "collectionExists": _collection_exists(bpy_ref, collection),
        "collectionObjectCount": len(objects) if objects is not None else 0,
        "cursor": cursor_values,
        "isDirty": bool(getattr(getattr(bpy_ref, "data", None), "is_dirty", False)),
    }


def check_import_preview(expected: dict, bpy_ref: Any) -> None:
    if import_preview(bpy_ref) != expected:
        raise SceneChangedError()


def _pointer(item: Any) -> int:
    as_pointer = getattr(item, "as_pointer", None)
    return int(as_pointer()) if callable(as_pointer) else id(item)


def snapshot_datablocks(bpy_ref: Any) -> dict:
    data = getattr(bpy_ref, "data", None)
    snapshot = {}
    for kind in _CREATED_DATA_KINDS:
        items = getattr(data, kind, None)
        if items is not None:
            snapshot[kind] = {_pointer(item) for item in items}
    return snapshot


def remove_datablocks_created_since(bpy_ref: Any, before: dict) -> None:
    """Remove only datablocks that did not exist in the `before` snapshot."""
    data = getattr(bpy_ref, "data", None)
    for kind in _CREATED_DATA_KINDS:
        items = getattr(data, kind, None)
        if items is None or kind not in before:
            continue
        for item in [item for item in items if _pointer(item) not in before[kind]]:
            try:
                items.remove(item)
            except Exception:
                pass


def import_model_asset(
    model: dict,
    cache: AssetCache,
    config: Optional[MintBlenderConfig] = None,
    urlopen: Optional[UrlOpen] = None,
    bpy_module: Optional[Any] = None,
    preview: Optional[dict] = None,
) -> Path:
    if not model.get("importSupported") or not model.get("importUrl"):
        raise ImportUnsupportedError("This Mint model does not have a durable GLB")

    asset_id = str(model.get("id") or "model")
    import_url = str(model["importUrl"])
    path = cache.path_for(asset_id, import_url, ".glb")
    if not path.exists():
        data = _read_url(import_url, config or MintBlenderConfig.from_env(), urlopen)
        path = cache.write_bytes(asset_id, import_url, data, ".glb")

    bpy_ref = bpy_module or _load_bpy()
    if preview is not None:
        check_import_preview(preview, bpy_ref)
    before_data = snapshot_datablocks(bpy_ref)
    before_ids = {id(obj) for obj in _scene_objects(bpy_ref)}
    try:
        bpy_ref.ops.import_scene.gltf(filepath=str(path))
        _rename_imported_asset(_imported_objects_after(bpy_ref, before_ids), model.get("name"))
    except Exception:
        remove_datablocks_created_since(bpy_ref, before_data)
        raise
    return path
