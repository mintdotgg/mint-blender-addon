"""Blender UI wrapper for the Mint asset browser."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import urllib.parse
import urllib.request
import webbrowser
import threading
from typing import Any, Callable, Iterable, Optional

from .browser import AssetBrowserState
from .cache import AssetCache
from .client import MintApiError, MintBlenderClient
from .config import MintBlenderConfig
from .http import UnsafeUrlError, ensure_safe_url, urlopen_with_config
from .auth import (
    RefreshTokenStore,
    StoredRefreshToken,
    exchange_authorization_code,
    exchange_refresh_token,
)
from .importer import (
    MODEL_IMPORT_LOCATION,
    ImportUnsupportedError,
    SceneChangedError,
    check_import_preview,
    import_model_asset,
    import_preview,
    remove_datablocks_created_since,
    snapshot_datablocks,
)
from .kiri_bridge import (
    KiriBridgeError,
    KiriBridgeUnavailableError,
    KIRI_SPLAT_MODE_CAMERA,
    KIRI_SPLAT_MODE_POINT_CLOUD,
    activate_kiri_splat_object,
    active_kiri_splat_object,
    create_kiri_insight_log,
    current_kiri_splat_display_mode,
    import_prepared_world_splat_with_kiri,
    kiri_insight_log_path,
    kiri_ready,
    kiri_splat_render_modifier,
    list_kiri_splat_objects,
    prepare_world_splat_ply,
    refine_kiri_splat_points,
    set_kiri_splat_display_mode,
    update_active_kiri_splat_view,
)
from .oauth import LoopbackCallbackServer, OAuthController

try:
    import bpy  # type: ignore
except ImportError:
    bpy = None

RUNTIME_STATE = AssetBrowserState()
_THUMBNAIL_PREVIEWS: Any = None
_WORLD_SPLAT_IMPORT_JOB: Any = None
_SHOW_ACTIVE_SPLAT_SECTION = True
_ACTIVE_SPLAT_AUTO_UPDATE = False
_ACTIVE_SPLAT_AUTO_UPDATE_OBJECT_NAME = ""
_ACTIVE_SPLAT_AUTO_UPDATE_LAST_SIGNATURE: Optional[tuple[Any, ...]] = None
_ACTIVE_SPLAT_AUTO_UPDATE_TIMER_REGISTERED = False
_ACTIVE_SPLAT_AUTO_UPDATE_INTERVAL_SECONDS = 3.0
KIRI_DOWNLOAD_URL = "https://github.com/Kiri-Innovation/3dgs-render-blender-addon"
WORLD_IMPORT_LOCATION = "KIRI 3DGS importer default"
BRAND_ICON_PATH = Path(__file__).resolve().parent / "assets" / "mint_icon_mint.png"
BRAND_ICON_PREVIEW_KEY = "mint_brand_icon"
BRAND_ICON_SCALE = 1.0
ASSET_THUMBNAIL_SCALE = 10.0
PACK_GRID_THUMBNAIL_SCALE = ASSET_THUMBNAIL_SCALE / 2

BrandIconLoader = Callable[[], Optional[int]]
ThumbnailLoader = Callable[[dict], Optional[int]]


class WorldSplatImportJob:
    def __init__(
        self,
        world: dict,
        access_token: str,
        config: MintBlenderConfig,
        log_path: Path,
        preview: Optional[dict] = None,
    ):
        self.world = dict(world)
        self.preview = preview
        self.access_token = access_token
        self.config = config
        self.log_path = log_path
        self.cache = AssetCache(config.cache_dir)
        self.client = MintBlenderClient(config)
        self.path: Optional[Path] = None
        self.error: Optional[Exception] = None
        self.done = False
        self.status = "Preparing..."
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def snapshot(self) -> tuple[bool, str, Optional[Path], Optional[Exception]]:
        with self._lock:
            return self.done, self.status, self.path, self.error

    def _set_status(self, status: str) -> None:
        with self._lock:
            self.status = status

    def _finish(self, path: Optional[Path], error: Optional[Exception]) -> None:
        with self._lock:
            self.path = path
            self.error = error
            self.done = True
            if error is None:
                self.status = "Importing..."
            else:
                self.status = "Failed"

    def _run(self) -> None:
        insight_log = create_kiri_insight_log(self.config)
        try:
            self._set_status("Preparing...")
            path = prepare_world_splat_ply(
                self.world,
                self.access_token,
                self.client,
                self.cache,
                config=self.config,
                insight_log=insight_log,
                check_kiri=False,
                status_callback=self._set_status,
            )
            self._finish(path, None)
        except Exception as error:
            self._finish(None, error)


def _clear_world_splat_import_job() -> None:
    global _WORLD_SPLAT_IMPORT_JOB
    _WORLD_SPLAT_IMPORT_JOB = None


def _redraw_mint_ui() -> None:
    if bpy is None:
        return
    screen = getattr(getattr(bpy, "context", None), "screen", None)
    for area in getattr(screen, "areas", []):
        if getattr(area, "type", None) == "VIEW_3D":
            area.tag_redraw()


def _brief_world_splat_status(status: str) -> str:
    normalized = status.lower()
    if "failed" in normalized:
        return "Failed"
    if "download" in normalized:
        return "Downloading..."
    if "cache" in normalized or "cached" in normalized:
        return "Cached..."
    if "import" in normalized or "kiri" in normalized:
        return "Importing..."
    if "convert" in normalized or "export" in normalized:
        return "Converting..."
    return "Preparing..."


def _world_splat_button_text(asset: dict) -> str:
    job = _WORLD_SPLAT_IMPORT_JOB
    asset_id = str(asset.get("id") or "")
    if job is None or str(job.world.get("id") or "") != asset_id:
        return "View Splat"

    _done, status, _path, _error = job.snapshot()
    return _brief_world_splat_status(status)


def _poll_world_splat_import_job() -> Optional[float]:
    job = _WORLD_SPLAT_IMPORT_JOB
    if job is None:
        return None

    done, status, path, error = job.snapshot()
    if not done:
        _redraw_mint_ui()
        return 0.5

    if error is not None or path is None:
        job._set_status("Failed")
        RUNTIME_STATE.status = "Splat import failed; log: %s" % job.log_path
        _redraw_mint_ui()
        return None

    preview = getattr(job, "preview", None)
    if preview is not None and bpy is not None:
        try:
            check_import_preview(preview, bpy)
        except SceneChangedError as changed:
            RUNTIME_STATE.status = str(changed)
            _clear_world_splat_import_job()
            _redraw_mint_ui()
            return None

    insight_log = create_kiri_insight_log(job.config)
    job._set_status("Importing...")
    _redraw_mint_ui()
    before_data = snapshot_datablocks(bpy) if bpy is not None else {}
    try:
        try:
            import_prepared_world_splat_with_kiri(
                path,
                job.world,
                config=job.config,
                insight_log=insight_log,
            )
        except Exception:
            if bpy is not None:
                remove_datablocks_created_since(bpy, before_data)
            raise
        if bpy is not None:
            try:
                bpy.ops.ed.undo_push(message="Import Mint World Splat")
            except Exception:
                pass
    except KiriBridgeUnavailableError as import_error:
        job._set_status("Failed")
        RUNTIME_STATE.status = "%s; log: %s" % (import_error, job.log_path)
    except KiriBridgeError as import_error:
        job._set_status("Failed")
        RUNTIME_STATE.status = "%s; log: %s" % (import_error, job.log_path)
    except Exception as import_error:
        job._set_status("Failed")
        RUNTIME_STATE.status = "Kiri import failed: %s; log: %s" % (
            import_error,
            job.log_path,
        )
    else:
        _clear_world_splat_import_job()
    _redraw_mint_ui()
    return None


def _register_world_splat_import_timer() -> None:
    if bpy is None:
        return
    bpy.app.timers.register(_poll_world_splat_import_job, first_interval=0.5)


def _matrix_values(value: Any) -> tuple[Any, ...]:
    try:
        rows = list(value)
    except Exception:
        return (repr(value),)

    values: list[Any] = []
    for row in rows:
        try:
            row_values = list(row)
        except Exception:
            row_values = [row]
        for item in row_values:
            try:
                values.append(round(float(item), 5))
            except (TypeError, ValueError):
                values.append(repr(item))
    return tuple(values)


def _active_view3d_update_context(
    bpy_ref: Any,
) -> tuple[Optional[tuple[Any, ...]], Optional[dict[str, Any]]]:
    screen = getattr(getattr(bpy_ref, "context", None), "screen", None)
    for area in getattr(screen, "areas", []) or []:
        if getattr(area, "type", None) != "VIEW_3D":
            continue

        spaces = getattr(area, "spaces", None)
        space = getattr(spaces, "active", None)
        if getattr(space, "type", None) != "VIEW_3D":
            for candidate in list(spaces or []):
                if getattr(candidate, "type", None) == "VIEW_3D":
                    space = candidate
                    break
        region_3d = getattr(space, "region_3d", None)
        if region_3d is None:
            continue

        region = None
        for candidate in getattr(area, "regions", []) or []:
            if getattr(candidate, "type", None) == "WINDOW":
                region = candidate
                break

        signature = (
            getattr(area, "width", None),
            getattr(area, "height", None),
            getattr(region_3d, "view_perspective", None),
            *_matrix_values(getattr(region_3d, "view_matrix", None)),
            *_matrix_values(getattr(region_3d, "window_matrix", None)),
        )
        override: dict[str, Any] = {"area": area, "space_data": space}
        if region is not None:
            override["region"] = region
        return signature, override
    return None, None


def _register_active_splat_auto_update_timer() -> None:
    global _ACTIVE_SPLAT_AUTO_UPDATE_TIMER_REGISTERED
    if bpy is None or _ACTIVE_SPLAT_AUTO_UPDATE_TIMER_REGISTERED:
        return
    bpy.app.timers.register(
        _poll_active_splat_auto_update,
        first_interval=_ACTIVE_SPLAT_AUTO_UPDATE_INTERVAL_SECONDS,
    )
    _ACTIVE_SPLAT_AUTO_UPDATE_TIMER_REGISTERED = True


def _set_active_splat_auto_update(enabled: bool, object_name: str = "") -> None:
    global _ACTIVE_SPLAT_AUTO_UPDATE
    global _ACTIVE_SPLAT_AUTO_UPDATE_OBJECT_NAME
    global _ACTIVE_SPLAT_AUTO_UPDATE_LAST_SIGNATURE
    _ACTIVE_SPLAT_AUTO_UPDATE = enabled
    _ACTIVE_SPLAT_AUTO_UPDATE_OBJECT_NAME = object_name if enabled else ""
    _ACTIVE_SPLAT_AUTO_UPDATE_LAST_SIGNATURE = None
    if enabled:
        _register_active_splat_auto_update_timer()


def _poll_active_splat_auto_update() -> Optional[float]:
    global _ACTIVE_SPLAT_AUTO_UPDATE_TIMER_REGISTERED
    global _ACTIVE_SPLAT_AUTO_UPDATE_LAST_SIGNATURE
    if not _ACTIVE_SPLAT_AUTO_UPDATE or bpy is None:
        _ACTIVE_SPLAT_AUTO_UPDATE_TIMER_REGISTERED = False
        return None

    try:
        active = active_kiri_splat_object(bpy)
        if _ACTIVE_SPLAT_AUTO_UPDATE_OBJECT_NAME and (
            active is None
            or _splat_name(active) != _ACTIVE_SPLAT_AUTO_UPDATE_OBJECT_NAME
        ):
            active = activate_kiri_splat_object(_ACTIVE_SPLAT_AUTO_UPDATE_OBJECT_NAME, bpy)
        if active is None:
            return _ACTIVE_SPLAT_AUTO_UPDATE_INTERVAL_SECONDS
        if not _splat_uses_camera_updates(active, bpy):
            return _ACTIVE_SPLAT_AUTO_UPDATE_INTERVAL_SECONDS

        signature, context_override = _active_view3d_update_context(bpy)
        if signature is None or signature == _ACTIVE_SPLAT_AUTO_UPDATE_LAST_SIGNATURE:
            return _ACTIVE_SPLAT_AUTO_UPDATE_INTERVAL_SECONDS

        _ACTIVE_SPLAT_AUTO_UPDATE_LAST_SIGNATURE = signature
        update_active_kiri_splat_view(bpy, context_override=context_override)
        _redraw_mint_ui()
    except Exception as error:
        RUNTIME_STATE.status = "Auto update failed: %s" % error
    return _ACTIVE_SPLAT_AUTO_UPDATE_INTERVAL_SECONDS


def _complete_oauth(
    config: MintBlenderConfig,
    token_store: RefreshTokenStore,
    callback: LoopbackCallbackServer,
    controller: OAuthController,
) -> None:
    try:
        session = controller.pending_session
        if session is None:
            return
        code = callback.wait_for_callback(session.state)
        token = exchange_authorization_code(
            config,
            code=code,
            code_verifier=session.verifier,
            redirect_uri=session.redirect_uri,
        )
        if token.refresh_token:
            token_store.save(
                StoredRefreshToken(
                    refresh_token=token.refresh_token,
                    client_id=config.client_id,
                    token_url=config.oauth_token_url,
                )
            )
        RUNTIME_STATE.set_connected(token.access_token)
        try:
            RUNTIME_STATE.refresh(MintBlenderClient(config))
        except Exception as error:
            RUNTIME_STATE.status = "Connected; refresh failed: %s" % error
    except Exception as error:
        RUNTIME_STATE.status = "Mint authorization failed: %s" % error
    finally:
        callback.close()


def _asset_name(asset: dict) -> str:
    return str(asset.get("name") or asset.get("id") or "Untitled")


def _asset_thumbnail_url(asset: dict) -> Optional[str]:
    assets = asset.get("assets")
    if not isinstance(assets, dict):
        return None

    for key in ("thumbnailUrl", "previewImageUrl", "panoUrl"):
        value = assets.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _pack_thumbnail_assets(pack: dict) -> list[dict]:
    assets = pack.get("assets")
    urls = assets.get("thumbnailUrls") if isinstance(assets, dict) else None
    thumbnails: list[dict] = []
    if isinstance(urls, list):
        for index, url in enumerate(urls[:4]):
            if isinstance(url, str) and url:
                thumbnails.append(
                    {
                        "id": "%s:thumbnail:%d" % (pack.get("id") or "pack", index),
                        "assets": {"thumbnailUrl": url},
                    }
                )

    if thumbnails:
        return thumbnails

    items = pack.get("items")
    if not isinstance(items, list):
        return []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_assets = item.get("assets")
        item_url = (
            item_assets.get("thumbnailUrl")
            if isinstance(item_assets, dict)
            else None
        )
        model = item.get("model")
        model_url = _asset_thumbnail_url(model) if isinstance(model, dict) else None
        url = item_url or model_url
        if isinstance(url, str) and url:
            thumbnails.append(
                {
                    "id": "%s:thumbnail:%d" % (pack.get("id") or "pack", len(thumbnails)),
                    "assets": {"thumbnailUrl": url},
                }
            )
        if len(thumbnails) >= 4:
            break
    return thumbnails


def _thumbnail_extension(url: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return suffix
    return ".img"


def _get_thumbnail_previews() -> Any:
    global _THUMBNAIL_PREVIEWS
    if _THUMBNAIL_PREVIEWS is None:
        previews = importlib.import_module("bpy.utils.previews")
        _THUMBNAIL_PREVIEWS = previews.new()
    return _THUMBNAIL_PREVIEWS


def _brand_icon_id() -> Optional[int]:
    if bpy is None or not BRAND_ICON_PATH.exists():
        return None

    try:
        previews = _get_thumbnail_previews()
        if BRAND_ICON_PREVIEW_KEY not in previews:
            previews.load(BRAND_ICON_PREVIEW_KEY, str(BRAND_ICON_PATH), "IMAGE")
        return int(previews[BRAND_ICON_PREVIEW_KEY].icon_id)
    except Exception:
        return None


def _download_thumbnail(
    asset_id: str,
    url: str,
    cache: AssetCache,
    config: MintBlenderConfig,
) -> Path:
    path = cache.path_for(asset_id, url, _thumbnail_extension(url))
    if path.exists():
        return path

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*,*/*",
            "User-Agent": "MintBlenderAssetBrowser/0.1.0",
        },
        method="GET",
    )
    response = urlopen_with_config(request, config, timeout=30)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.read())
    return path


def _section_icon(is_open: bool) -> str:
    return "TRIA_DOWN" if is_open else "TRIA_RIGHT"


def _thumbnail_icon_id(asset: dict) -> Optional[int]:
    if bpy is None:
        return None

    url = _asset_thumbnail_url(asset)
    if not url:
        return None

    try:
        config = MintBlenderConfig.from_env()
        asset_id = str(asset.get("id") or "asset")
        cache = AssetCache(config.cache_dir / "thumbnails")
        path = _download_thumbnail(asset_id, url, cache, config)
        preview_key = "%s:%s" % (asset_id, url)
        previews = _get_thumbnail_previews()
        if preview_key not in previews:
            previews.load(preview_key, str(path), "IMAGE")
        return int(previews[preview_key].icon_id)
    except Exception:
        return None


def _draw_asset(
    layout: Any,
    asset: dict,
    is_model: bool,
    thumbnail_loader: Optional[ThumbnailLoader] = None,
    world_import_available: bool = True,
) -> None:
    box = layout.box()
    thumbnail_icon_id = thumbnail_loader(asset) if thumbnail_loader else None
    if thumbnail_icon_id is not None:
        box.template_icon(icon_value=thumbnail_icon_id, scale=ASSET_THUMBNAIL_SCALE)
    box.label(text=_asset_name(asset))

    action_row = box.row(align=True)
    if is_model and asset.get("importSupported"):
        op = action_row.operator("mint_asset_browser.import_model", text="Import GLB")
        op.asset_id = str(asset.get("id"))
    else:
        if is_model:
            action_row.label(text="No durable GLB yet")
        elif world_import_available:
            op = action_row.operator(
                "mint_asset_browser.import_world_kiri",
                text=_world_splat_button_text(asset),
            )
            op.asset_id = str(asset.get("id"))
        else:
            action_row.label(text="Kiri required")
            op = action_row.operator("mint_asset_browser.open_in_mint", text="Get Kiri", icon="LINKED")
            op.url = KIRI_DOWNLOAD_URL

    mint_url = asset.get("mintUrl")
    if mint_url:
        op = action_row.operator("mint_asset_browser.open_in_mint", text="", icon="LINKED")
        op.url = str(mint_url)


def _draw_pack(
    layout: Any,
    pack: dict,
    thumbnail_loader: Optional[ThumbnailLoader] = None,
) -> None:
    box = layout.box()
    thumbnails = _pack_thumbnail_assets(pack)
    if thumbnail_loader and thumbnails:
        if len(thumbnails) == 1:
            thumbnail_icon_id = thumbnail_loader(thumbnails[0])
            if thumbnail_icon_id is not None:
                box.template_icon(icon_value=thumbnail_icon_id, scale=ASSET_THUMBNAIL_SCALE)
        else:
            for start in range(0, min(len(thumbnails), 4), 2):
                row = box.row(align=True)
                for thumbnail in thumbnails[start : start + 2]:
                    thumbnail_icon_id = thumbnail_loader(thumbnail)
                    if thumbnail_icon_id is not None:
                        row.template_icon(icon_value=thumbnail_icon_id, scale=PACK_GRID_THUMBNAIL_SCALE)

    box.label(text=_asset_name(pack))
    model_count = int(pack.get("modelCount") or 0)
    box.label(text="%d models" % model_count if model_count != 1 else "1 model")

    action_row = box.row(align=True)
    op = action_row.operator("mint_asset_browser.open_pack", text="Open Pack")
    op.pack_id = str(pack.get("id"))
    mint_url = pack.get("mintUrl")
    if mint_url:
        op = action_row.operator("mint_asset_browser.open_in_mint", text="", icon="LINKED")
        op.url = str(mint_url)


def _draw_section_toggle(layout: Any, text: str, section: str, is_open: bool) -> None:
    op = layout.operator(
        "mint_asset_browser.toggle_section",
        text=text,
        icon=_section_icon(is_open),
        emboss=False,
    )
    op.section = section


def _draw_mint_panel_header(
    layout: Any,
    icon_loader: Optional[BrandIconLoader] = None,
) -> None:
    loader = icon_loader or _brand_icon_id
    try:
        icon_id = loader()
    except Exception:
        icon_id = None

    if icon_id is not None:
        layout.template_icon(icon_value=icon_id, scale=BRAND_ICON_SCALE)


def _kiri_ready_for_ui() -> bool:
    if bpy is None:
        return False
    try:
        return kiri_ready(bpy)
    except Exception:
        return False


def _splat_name(obj: Any) -> str:
    return str(getattr(obj, "name", "") or "Untitled Splat")


def _splat_is_point_cloud(active: Any, bpy_module: Optional[Any] = None) -> bool:
    try:
        return current_kiri_splat_display_mode(active, bpy_module) == KIRI_SPLAT_MODE_POINT_CLOUD
    except Exception:
        return False


def _splat_uses_camera_updates(active: Any, bpy_module: Optional[Any] = None) -> bool:
    try:
        return current_kiri_splat_display_mode(active, bpy_module) == KIRI_SPLAT_MODE_CAMERA
    except Exception:
        return False


def draw_active_splat_panel(layout: Any, bpy_module: Optional[Any] = None) -> None:
    bpy_ref = bpy_module if bpy_module is not None else bpy
    if bpy_ref is None:
        layout.label(text="Kiri unavailable")
        return

    try:
        if not kiri_ready(bpy_ref):
            box = layout.box()
            box.label(text="Kiri unavailable")
            op = box.operator("mint_asset_browser.open_in_mint", text="Get Kiri", icon="LINKED")
            op.url = KIRI_DOWNLOAD_URL
            return
        splats = list_kiri_splat_objects(bpy_ref)
        active = active_kiri_splat_object(bpy_ref)
    except Exception as error:
        box = layout.box()
        box.label(text="Splat controls unavailable")
        box.label(text=str(error)[:80])
        return

    if not splats:
        layout.label(text="No splats in scene")
        return

    if active is None:
        active = splats[0]

    box = layout.box()
    box.label(text="Active: %s" % _splat_name(active))

    mode_row = box.row(align=True)
    camera_op = mode_row.operator(
        "mint_asset_browser.set_splat_mode",
        text="Render Mode",
        depress=_splat_uses_camera_updates(active, bpy_ref),
    )
    camera_op.object_name = _splat_name(active)
    camera_op.mode = KIRI_SPLAT_MODE_CAMERA

    edit_op = mode_row.operator(
        "mint_asset_browser.activate_splat",
        text="Point Cloud",
        depress=_splat_is_point_cloud(active, bpy_ref),
    )
    edit_op.object_name = _splat_name(active)

    if _splat_is_point_cloud(active, bpy_ref):
        point_row = box.row(align=True)
        modifier = kiri_splat_render_modifier(active)
        if modifier is not None:
            point_row.prop(modifier, '["Socket_51"]', text="Point Size")
        fine_op = point_row.operator("mint_asset_browser.fine_splat_points", text="Finer")
        fine_op.object_name = _splat_name(active)

    update_row = box.row(align=True)
    update_op = update_row.operator("mint_asset_browser.update_splat_view", text="Update View")
    update_op.object_name = _splat_name(active)
    auto_op = update_row.operator(
        "mint_asset_browser.toggle_splat_auto_update",
        text="Auto Update",
        depress=(
            _ACTIVE_SPLAT_AUTO_UPDATE
            and _ACTIVE_SPLAT_AUTO_UPDATE_OBJECT_NAME == _splat_name(active)
        ),
    )
    auto_op.object_name = _splat_name(active)

    if len(splats) <= 1:
        return

    for splat in splats:
        if splat is active:
            continue
        row = box.row(align=True)
        row.label(text=_splat_name(splat))
        op = row.operator("mint_asset_browser.activate_splat", text="Edit")
        op.object_name = _splat_name(splat)


def _draw_account_section(layout: Any, state: AssetBrowserState) -> None:
    _draw_section_toggle(
        layout,
        "My Account",
        "account",
        state.show_account_section,
    )
    if not state.show_account_section:
        return

    box = layout.box()
    row = box.row(align=True)
    row.operator("mint_asset_browser.connect", text="Log In")
    row.operator("mint_asset_browser.disconnect", text="Log Out")
    row.operator("mint_asset_browser.refresh", text="Refresh")
    box.label(text=state.status)


def _draw_active_splat_section(layout: Any) -> None:
    _draw_section_toggle(
        layout,
        "Active Splat",
        "active_splat",
        _SHOW_ACTIVE_SPLAT_SECTION,
    )
    if _SHOW_ACTIVE_SPLAT_SECTION:
        draw_active_splat_panel(layout)


def _draw_assets_section(layout: Any, state: AssetBrowserState) -> None:
    _draw_section_toggle(
        layout,
        "My Mint Assets",
        "assets",
        state.show_assets_section,
    )
    if not state.show_assets_section:
        return

    box = layout.box()
    tab_row = box.row()
    tab_row.operator("mint_asset_browser.show_models", text="Models")
    tab_row.operator("mint_asset_browser.show_worlds", text="Worlds")
    tab_row.operator("mint_asset_browser.show_packs", text="Packs")

    rows: Iterable[dict]
    if state.active_tab == "packs":
        selected_pack = state.selected_pack()
        if selected_pack:
            header = box.row(align=True)
            header.operator("mint_asset_browser.back_to_packs", text="", icon="TRIA_LEFT")
            header.label(text=_asset_name(selected_pack))
            rows = state.pack_models(selected_pack)
            for asset in rows:
                _draw_asset(
                    box,
                    asset,
                    is_model=True,
                    thumbnail_loader=_thumbnail_icon_id if bpy is not None else None,
                )
        else:
            rows = state.filtered_packs()
            for pack in rows:
                _draw_pack(
                    box,
                    pack,
                    thumbnail_loader=_thumbnail_icon_id if bpy is not None else None,
                )
        return

    is_model = state.active_tab == "models"
    rows = state.filtered_models() if is_model else state.filtered_worlds()
    world_import_available = True if is_model else _kiri_ready_for_ui()
    for asset in rows:
        _draw_asset(
            box,
            asset,
            is_model=is_model,
            thumbnail_loader=_thumbnail_icon_id if bpy is not None else None,
            world_import_available=world_import_available,
        )


def draw_asset_browser(layout: Any, state: AssetBrowserState = RUNTIME_STATE) -> None:
    _draw_account_section(layout, state)
    _draw_active_splat_section(layout)
    _draw_assets_section(layout, state)


def _save_refresh_token(
    config: MintBlenderConfig,
    token_store: RefreshTokenStore,
    refresh_token: str,
) -> None:
    token_store.save(
        StoredRefreshToken(
            refresh_token=refresh_token,
            client_id=config.client_id,
            token_url=config.oauth_token_url,
        )
    )


def _restore_access_token_from_store(
    config: MintBlenderConfig,
    state: AssetBrowserState,
    store: RefreshTokenStore,
) -> None:
    stored = store.load()
    if not stored:
        raise MintApiError(401, "Connect Mint and try again.") from None
    if stored.client_id != config.client_id or stored.token_url != config.oauth_token_url:
        raise MintApiError(401, "Reconnect Mint for this local Blender session.") from None

    token = exchange_refresh_token(config, stored.refresh_token)
    if token.refresh_token:
        _save_refresh_token(config, store, token.refresh_token)
    state.set_connected(token.access_token)


def refresh_with_stored_token(
    config: MintBlenderConfig,
    state: AssetBrowserState = RUNTIME_STATE,
    token_store: Optional[RefreshTokenStore] = None,
    client: Optional[MintBlenderClient] = None,
) -> None:
    store = token_store or RefreshTokenStore(config.token_store_path)
    api_client = client or MintBlenderClient(config)
    if state.access_token:
        try:
            state.refresh(api_client)
            return
        except MintApiError as error:
            if error.status != 401:
                raise

    _restore_access_token_from_store(config, state, store)
    state.refresh(api_client)


def preview_lines(asset_name: str, preview: dict, location: str) -> list[str]:
    return [
        "Import: %s" % asset_name,
        "Into collection: %s" % (preview.get("collection") or "(none)"),
        "Location: %s" % location,
    ]


def _draw_preview(layout: Any, asset_name: str, preview_json: str, location: str) -> None:
    try:
        preview = json.loads(preview_json) if preview_json else {}
    except ValueError:
        preview = {}
    for line in preview_lines(asset_name, preview, location):
        layout.label(text=line)


if bpy is not None:

    class MINT_OT_toggle_section(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.toggle_section"
        bl_label = "Toggle Mint Section"
        bl_description = "Show or hide this Mint panel section"

        section: bpy.props.StringProperty(default="")  # type: ignore

        def execute(self, context):  # type: ignore
            global _SHOW_ACTIVE_SPLAT_SECTION
            if self.section == "account":
                RUNTIME_STATE.show_account_section = not RUNTIME_STATE.show_account_section
            elif self.section == "active_splat":
                _SHOW_ACTIVE_SPLAT_SECTION = not _SHOW_ACTIVE_SPLAT_SECTION
            elif self.section == "assets":
                RUNTIME_STATE.show_assets_section = not RUNTIME_STATE.show_assets_section
            return {"FINISHED"}

    class MINT_OT_connect(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.connect"
        bl_label = "Connect Mint"
        bl_description = "Connect Blender to Mint and load your assets"

        def execute(self, context):  # type: ignore
            config = MintBlenderConfig.from_env()
            token_store = RefreshTokenStore(config.token_store_path)
            try:
                refresh_with_stored_token(config, token_store=token_store)
                return {"FINISHED"}
            except Exception:
                pass

            controller = OAuthController(
                config,
                token_store,
            )
            callback = LoopbackCallbackServer(
                host=config.loopback_host,
                path=config.loopback_callback_path,
            )
            controller.start(
                open_browser=webbrowser.open,
                redirect_uri=callback.redirect_uri,
            )
            threading.Thread(
                target=_complete_oauth,
                args=(config, token_store, callback, controller),
                daemon=True,
            ).start()
            RUNTIME_STATE.status = "Mint authorization opened in browser"
            return {"FINISHED"}

    class MINT_OT_disconnect(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.disconnect"
        bl_label = "Disconnect Mint"
        bl_description = "Disconnect Mint and clear the local session"

        def execute(self, context):  # type: ignore
            config = MintBlenderConfig.from_env()
            OAuthController(config, RefreshTokenStore(config.token_store_path)).disconnect()
            RUNTIME_STATE.disconnect()
            return {"FINISHED"}

    class MINT_OT_refresh(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.refresh"
        bl_label = "Refresh Mint Assets"
        bl_description = "Reload your latest Mint models, worlds, and packs"

        def execute(self, context):  # type: ignore
            config = MintBlenderConfig.from_env()
            try:
                refresh_with_stored_token(config)
            except Exception as error:
                RUNTIME_STATE.status = "Refresh failed: %s" % error
                return {"CANCELLED"}
            return {"FINISHED"}

    class MINT_OT_show_models(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.show_models"
        bl_label = "Show Mint Models"
        bl_description = "Show succeeded Mint 3D models"

        def execute(self, context):  # type: ignore
            RUNTIME_STATE.active_tab = "models"
            RUNTIME_STATE.selected_pack_id = None
            return {"FINISHED"}

    class MINT_OT_show_worlds(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.show_worlds"
        bl_label = "Show Mint Worlds"
        bl_description = "Show succeeded Mint worlds"

        def execute(self, context):  # type: ignore
            RUNTIME_STATE.active_tab = "worlds"
            RUNTIME_STATE.selected_pack_id = None
            return {"FINISHED"}

    class MINT_OT_show_packs(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.show_packs"
        bl_label = "Show Mint Packs"
        bl_description = "Show Mint asset packs with generated 3D models"

        def execute(self, context):  # type: ignore
            RUNTIME_STATE.active_tab = "packs"
            RUNTIME_STATE.selected_pack_id = None
            return {"FINISHED"}

    class MINT_OT_open_pack(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.open_pack"
        bl_label = "Open Mint Pack"
        bl_description = "Show this pack's generated model assets"

        pack_id: bpy.props.StringProperty(default="")  # type: ignore

        def execute(self, context):  # type: ignore
            RUNTIME_STATE.active_tab = "packs"
            RUNTIME_STATE.selected_pack_id = self.pack_id
            return {"FINISHED"}

    class MINT_OT_back_to_packs(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.back_to_packs"
        bl_label = "Back to Mint Packs"
        bl_description = "Return to the Mint pack list"

        def execute(self, context):  # type: ignore
            RUNTIME_STATE.active_tab = "packs"
            RUNTIME_STATE.selected_pack_id = None
            return {"FINISHED"}

    class MINT_OT_open_in_mint(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.open_in_mint"
        bl_label = "Open in Mint"
        bl_description = "Open this asset in Mint"

        url: bpy.props.StringProperty(default="")  # type: ignore

        def execute(self, context):  # type: ignore
            if self.url:
                try:
                    webbrowser.open(ensure_safe_url(self.url))
                except UnsafeUrlError as error:
                    RUNTIME_STATE.status = str(error)
                    return {"CANCELLED"}
            return {"FINISHED"}

    class MINT_OT_import_model(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.import_model"
        bl_label = "Import Mint Model"
        bl_description = "Download and import this model GLB into Blender"
        bl_options = {"REGISTER", "UNDO"}

        asset_id: bpy.props.StringProperty(default="")  # type: ignore
        preview: bpy.props.StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})  # type: ignore

        def invoke(self, context, event):  # type: ignore
            if not RUNTIME_STATE.find_model(self.asset_id):
                RUNTIME_STATE.status = "Model not found"
                return {"CANCELLED"}
            self.preview = json.dumps(import_preview(bpy))
            return context.window_manager.invoke_props_dialog(self)

        def draw(self, context):  # type: ignore
            model = RUNTIME_STATE.find_model(self.asset_id) or {}
            _draw_preview(self.layout, _asset_name(model), self.preview, MODEL_IMPORT_LOCATION)

        def execute(self, context):  # type: ignore
            config = MintBlenderConfig.from_env()
            model = RUNTIME_STATE.find_model(self.asset_id)
            if not model:
                RUNTIME_STATE.status = "Model not found"
                return {"CANCELLED"}
            try:
                import_model_asset(
                    model,
                    AssetCache(config.cache_dir),
                    config=config,
                    preview=json.loads(self.preview) if self.preview else None,
                )
            except SceneChangedError as error:
                RUNTIME_STATE.status = str(error)
                self.report({"WARNING"}, str(error))
                return {"CANCELLED"}
            except ImportUnsupportedError as error:
                RUNTIME_STATE.status = str(error)
                return {"CANCELLED"}
            except Exception as error:
                RUNTIME_STATE.status = "Import failed: %s" % error
                return {"CANCELLED"}
            RUNTIME_STATE.status = "Imported %s" % _asset_name(model)
            return {"FINISHED"}

    class MINT_OT_import_world_kiri(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.import_world_kiri"
        bl_label = "Import Mint World Splat"
        bl_description = "View splat"

        asset_id: bpy.props.StringProperty(default="")  # type: ignore
        preview: bpy.props.StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})  # type: ignore

        def invoke(self, context, event):  # type: ignore
            job = _WORLD_SPLAT_IMPORT_JOB
            if job is not None and not job.snapshot()[0]:
                return self.execute(context)
            if not RUNTIME_STATE.find_world(self.asset_id):
                RUNTIME_STATE.status = "World not found"
                return {"CANCELLED"}
            self.preview = json.dumps(import_preview(bpy))
            return context.window_manager.invoke_props_dialog(self)

        def draw(self, context):  # type: ignore
            world = RUNTIME_STATE.find_world(self.asset_id) or {}
            _draw_preview(self.layout, _asset_name(world), self.preview, WORLD_IMPORT_LOCATION)

        def execute(self, context):  # type: ignore
            global _WORLD_SPLAT_IMPORT_JOB
            config = MintBlenderConfig.from_env()
            world = RUNTIME_STATE.find_world(self.asset_id)
            if not world:
                RUNTIME_STATE.status = "World not found"
                return {"CANCELLED"}
            if not RUNTIME_STATE.access_token:
                RUNTIME_STATE.status = "Connect to Mint first"
                return {"CANCELLED"}

            if _WORLD_SPLAT_IMPORT_JOB is not None:
                done, status, _path, _error = _WORLD_SPLAT_IMPORT_JOB.snapshot()
                if not done:
                    _redraw_mint_ui()
                    return {"FINISHED"}
                _clear_world_splat_import_job()

            log_path = kiri_insight_log_path(config)
            insight_log = create_kiri_insight_log(config)
            try:
                insight_log.log("load_bpy_start", providedBpyModule=False)
                ready = kiri_ready()
                insight_log.log("kiri_ready_check", ready=ready)
                if not ready:
                    raise KiriBridgeUnavailableError(
                        "Install and enable KIRI Engine 3DGS Render to import worlds"
                    )
            except KiriBridgeUnavailableError as error:
                RUNTIME_STATE.status = "%s; log: %s" % (error, log_path)
                return {"CANCELLED"}
            except KiriBridgeError as error:
                RUNTIME_STATE.status = "%s; log: %s" % (error, log_path)
                return {"CANCELLED"}
            except Exception as error:
                RUNTIME_STATE.status = "Kiri import failed: %s; log: %s" % (error, log_path)
                return {"CANCELLED"}

            _WORLD_SPLAT_IMPORT_JOB = WorldSplatImportJob(
                world,
                RUNTIME_STATE.access_token,
                config,
                log_path,
                preview=json.loads(self.preview) if self.preview else None,
            )
            _WORLD_SPLAT_IMPORT_JOB.start()
            _register_world_splat_import_timer()
            _redraw_mint_ui()
            return {"FINISHED"}

    class MINT_OT_activate_splat(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.activate_splat"
        bl_label = "Select Mint Splat"
        bl_description = "Select this splat mesh and show it as editable points"

        object_name: bpy.props.StringProperty(default="")  # type: ignore

        def execute(self, context):  # type: ignore
            try:
                set_kiri_splat_display_mode(
                    self.object_name,
                    KIRI_SPLAT_MODE_POINT_CLOUD,
                )
                refine_kiri_splat_points(self.object_name)
                _set_active_splat_auto_update(False)
            except Exception as error:
                RUNTIME_STATE.status = "Select splat failed: %s" % error
                return {"CANCELLED"}
            _redraw_mint_ui()
            return {"FINISHED"}

    class MINT_OT_set_splat_mode(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.set_splat_mode"
        bl_label = "Set Mint Splat Mode"
        bl_description = "Switch the active splat display mode"

        mode: bpy.props.EnumProperty(  # type: ignore
            items=[
                (KIRI_SPLAT_MODE_CAMERA, "Render Mode", "Show Kiri's camera-updated preview"),
                (KIRI_SPLAT_MODE_POINT_CLOUD, "Point Cloud", "Show editable points"),
            ],
            default=KIRI_SPLAT_MODE_CAMERA,
        )
        object_name: bpy.props.StringProperty(default="")  # type: ignore

        def execute(self, context):  # type: ignore
            object_name = self.object_name
            if not object_name:
                active = active_kiri_splat_object()
                object_name = _splat_name(active) if active is not None else ""
            if not object_name:
                RUNTIME_STATE.status = "No active splat"
                return {"CANCELLED"}
            try:
                set_kiri_splat_display_mode(object_name, self.mode)
                if self.mode == KIRI_SPLAT_MODE_POINT_CLOUD:
                    _set_active_splat_auto_update(False)
            except Exception as error:
                RUNTIME_STATE.status = "Splat mode failed: %s" % error
                return {"CANCELLED"}
            _redraw_mint_ui()
            return {"FINISHED"}

    class MINT_OT_fine_splat_points(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.fine_splat_points"
        bl_label = "Make Mint Splat Points Finer"
        bl_description = "Reduce the active point cloud point size"

        object_name: bpy.props.StringProperty(default="")  # type: ignore

        def execute(self, context):  # type: ignore
            object_name = self.object_name
            if not object_name:
                active = active_kiri_splat_object()
                object_name = _splat_name(active) if active is not None else ""
            if not object_name:
                RUNTIME_STATE.status = "No active splat"
                return {"CANCELLED"}
            try:
                set_kiri_splat_display_mode(object_name, KIRI_SPLAT_MODE_POINT_CLOUD)
                refine_kiri_splat_points(object_name)
            except Exception as error:
                RUNTIME_STATE.status = "Point size failed: %s" % error
                return {"CANCELLED"}
            _redraw_mint_ui()
            return {"FINISHED"}

    class MINT_OT_update_splat_view(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.update_splat_view"
        bl_label = "Update Mint Splat View"
        bl_description = "Update the active splat to the current view"

        object_name: bpy.props.StringProperty(default="")  # type: ignore

        def execute(self, context):  # type: ignore
            try:
                if self.object_name:
                    activate_kiri_splat_object(self.object_name)
                _signature, context_override = _active_view3d_update_context(bpy)
                update_active_kiri_splat_view(context_override=context_override)
            except Exception as error:
                RUNTIME_STATE.status = "Update view failed: %s" % error
                return {"CANCELLED"}
            _redraw_mint_ui()
            return {"FINISHED"}

    class MINT_OT_toggle_splat_auto_update(bpy.types.Operator):  # type: ignore
        bl_idname = "mint_asset_browser.toggle_splat_auto_update"
        bl_label = "Toggle Mint Splat Auto Update"
        bl_description = "Automatically update the active splat when the viewport moves"

        object_name: bpy.props.StringProperty(default="")  # type: ignore

        def execute(self, context):  # type: ignore
            object_name = self.object_name
            if not object_name:
                active = active_kiri_splat_object()
                object_name = _splat_name(active) if active is not None else ""
            if not object_name:
                RUNTIME_STATE.status = "No active splat"
                return {"CANCELLED"}

            enable = not (
                _ACTIVE_SPLAT_AUTO_UPDATE
                and _ACTIVE_SPLAT_AUTO_UPDATE_OBJECT_NAME == object_name
            )
            if not enable:
                _set_active_splat_auto_update(False)
                _redraw_mint_ui()
                return {"FINISHED"}

            try:
                set_kiri_splat_display_mode(object_name, KIRI_SPLAT_MODE_CAMERA)
                _set_active_splat_auto_update(True, object_name)
            except Exception as error:
                RUNTIME_STATE.status = "Auto update failed: %s" % error
                return {"CANCELLED"}

            _redraw_mint_ui()
            return {"FINISHED"}

    class MINT_PT_asset_browser(bpy.types.Panel):  # type: ignore
        bl_idname = "MINT_PT_asset_browser"
        bl_label = "Mint"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Mint"

        def draw_header(self, context):  # type: ignore
            _draw_mint_panel_header(self.layout)

        def draw(self, context):  # type: ignore
            draw_asset_browser(self.layout, RUNTIME_STATE)

    CLASSES = [
        MINT_OT_toggle_section,
        MINT_OT_connect,
        MINT_OT_disconnect,
        MINT_OT_refresh,
        MINT_OT_show_models,
        MINT_OT_show_worlds,
        MINT_OT_show_packs,
        MINT_OT_open_pack,
        MINT_OT_back_to_packs,
        MINT_OT_open_in_mint,
        MINT_OT_import_model,
        MINT_OT_import_world_kiri,
        MINT_OT_activate_splat,
        MINT_OT_set_splat_mode,
        MINT_OT_fine_splat_points,
        MINT_OT_update_splat_view,
        MINT_OT_toggle_splat_auto_update,
        MINT_PT_asset_browser,
    ]
else:
    CLASSES = []


def register() -> None:
    if bpy is None:
        return
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    if bpy is None:
        return
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
    global _THUMBNAIL_PREVIEWS
    if _THUMBNAIL_PREVIEWS is not None:
        previews = importlib.import_module("bpy.utils.previews")
        previews.remove(_THUMBNAIL_PREVIEWS)
        _THUMBNAIL_PREVIEWS = None
