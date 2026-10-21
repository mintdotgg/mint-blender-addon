import unittest
from pathlib import Path

from mint_blender_asset_browser.browser import AssetBrowserState
from mint_blender_asset_browser.auth import StoredRefreshToken, TokenResponse
from mint_blender_asset_browser.client import MintApiError
from mint_blender_asset_browser.config import MintBlenderConfig
from mint_blender_asset_browser.ui import (
    ASSET_THUMBNAIL_SCALE,
    BRAND_ICON_SCALE,
    KIRI_DOWNLOAD_URL,
    _asset_thumbnail_url,
    _draw_asset,
    _draw_mint_panel_header,
    _thumbnail_extension,
    draw_active_splat_panel,
    draw_asset_browser,
    refresh_with_stored_token,
)
import mint_blender_asset_browser.ui as ui_module


class OperatorCall:
    def __init__(self, operator_id, text, **kwargs):
        self.operator_id = operator_id
        self.text = text
        self.kwargs = kwargs


class MenuCall:
    def __init__(self, operator_id, property_name, text, **kwargs):
        self.operator_id = operator_id
        self.property_name = property_name
        self.text = text
        self.kwargs = kwargs


class FakeLayout:
    def __init__(self):
        self.labels = []
        self.operators = []
        self.operator_menus = []
        self.props = []
        self.icons = []
        self.events = []

    def row(self, *args, **kwargs):
        return self

    def column(self, *args, **kwargs):
        return self

    def box(self):
        return self

    def label(self, text):
        self.labels.append(text)
        self.events.append(("label", text))

    def operator(self, operator_id, text, **kwargs):
        call = OperatorCall(operator_id, text, **kwargs)
        self.operators.append(call)
        self.events.append(("operator", text))
        return call

    def operator_menu_enum(self, operator_id, property_name, text, **kwargs):
        call = MenuCall(operator_id, property_name, text, **kwargs)
        self.operator_menus.append(call)
        return call

    def prop(self, data, property_name, text, **kwargs):
        self.props.append((data, property_name, text, kwargs))

    def template_icon(self, icon_value, scale=1.0):
        self.icons.append((icon_value, scale))


class FakeKiriProps:
    def __init__(self):
        self.import_face_vert = None
        self.active_mode = "Edit"


class FakeKiriObjectProps:
    def __init__(self, update_mode="Disable Camera Updates"):
        self.update_mode = update_mode
        self.cam_update = False


class FakeModifier:
    def __init__(self):
        self.name = "KIRI_3DGS_Render_GN"
        self.values = {"Socket_51": 1.0}

    def __getitem__(self, key):
        return self.values[key]

    def __setitem__(self, key, value):
        self.values[key] = value


class FakeSplat:
    def __init__(self, name, update_mode="Disable Camera Updates"):
        self.name = name
        self.modifiers = [FakeModifier()]
        self.values = {"3DGS_Mesh_Type": "3DGS"}
        self.sna_dgs_object_properties = FakeKiriObjectProps(update_mode)

    def get(self, key):
        return self.values.get(key)

    def select_set(self, selected):
        self.selected = selected


class FakeViewLayerObjects:
    def __init__(self):
        self.active = None


class FakeViewLayer:
    def __init__(self):
        self.objects = FakeViewLayerObjects()


class FakeKiriScene:
    def __init__(self, objects=None, with_props=True):
        self.objects = objects or []
        if with_props:
            self.sna_dgs_scene_properties = FakeKiriProps()


class FakeKiriContext:
    def __init__(self, objects=None, with_props=True):
        self.scene = FakeKiriScene(objects, with_props)
        self.view_layer = FakeViewLayer()


class FakeKiriSnaOps:
    def dgs_render_import_ply_e0a3a(self, filepath):
        return {"FINISHED"}


class FakeKiriOps:
    def __init__(self, with_operator=True):
        self.sna = FakeKiriSnaOps() if with_operator else object()


class FakeKiriBpy:
    def __init__(self, objects=None, with_props=True, with_operator=True):
        self.context = FakeKiriContext(objects, with_props)
        self.ops = FakeKiriOps(with_operator)


class UiTest(unittest.TestCase):
    def tearDown(self):
        ui_module._WORLD_SPLAT_IMPORT_JOB = None
        ui_module._SHOW_ACTIVE_SPLAT_SECTION = True
        ui_module._ACTIVE_SPLAT_AUTO_UPDATE = False
        ui_module._ACTIVE_SPLAT_AUTO_UPDATE_OBJECT_NAME = ""
        ui_module._ACTIVE_SPLAT_AUTO_UPDATE_LAST_SIGNATURE = None
        ui_module._ACTIVE_SPLAT_AUTO_UPDATE_TIMER_REGISTERED = False

    def test_draw_model_tab_exposes_import_for_durable_models(self):
        state = AssetBrowserState(
            status="Loaded",
            active_tab="models",
            models=[
                {
                    "id": "model_1",
                    "name": "Chair",
                    "status": "succeeded",
                    "importSupported": True,
                    "mintUrl": "https://mint.gg/0xAlice/model_1",
                }
            ],
        )
        layout = FakeLayout()

        draw_asset_browser(layout, state)

        operator_ids = [call.operator_id for call in layout.operators]
        self.assertIn("mint_asset_browser.connect", operator_ids)
        self.assertIn("mint_asset_browser.refresh", operator_ids)
        self.assertIn("mint_asset_browser.import_model", operator_ids)
        self.assertIn("My Account", [call.text for call in layout.operators])
        self.assertIn("My Mint Assets", [call.text for call in layout.operators])
        self.assertIn("Log In", [call.text for call in layout.operators])
        self.assertIn("Log Out", [call.text for call in layout.operators])
        self.assertIn("Chair", layout.labels)
        self.assertNotIn("Status: succeeded", layout.labels)

    def test_draw_asset_renders_thumbnail_icon_when_available(self):
        layout = FakeLayout()

        _draw_asset(
            layout,
            {
                "id": "model_1",
                "name": "Chair",
                "status": "succeeded",
                "importSupported": True,
            },
            is_model=True,
            thumbnail_loader=lambda asset: 42,
        )

        self.assertEqual(layout.icons, [(42, ASSET_THUMBNAIL_SCALE)])
        self.assertIn("Chair", layout.labels)
        self.assertNotIn("Status: succeeded", layout.labels)

    def test_draw_mint_panel_header_uses_packaged_icon_when_available(self):
        layout = FakeLayout()

        _draw_mint_panel_header(layout, icon_loader=lambda: 77)

        self.assertEqual(layout.icons, [(77, BRAND_ICON_SCALE)])
        self.assertEqual(layout.labels, [])

    def test_asset_thumbnail_url_prefers_thumbnail_then_preview(self):
        self.assertEqual(
            _asset_thumbnail_url(
                {
                    "assets": {
                        "thumbnailUrl": "https://cdn.mint.gg/thumb.webp",
                        "previewImageUrl": "https://cdn.mint.gg/preview.png",
                    }
                }
            ),
            "https://cdn.mint.gg/thumb.webp",
        )
        self.assertEqual(
            _asset_thumbnail_url(
                {
                    "assets": {
                        "thumbnailUrl": None,
                        "previewImageUrl": "https://cdn.mint.gg/preview.png",
                    }
                }
            ),
            "https://cdn.mint.gg/preview.png",
        )
        self.assertIsNone(_asset_thumbnail_url({"assets": None}))

    def test_thumbnail_extension_uses_supported_image_suffixes(self):
        self.assertEqual(_thumbnail_extension("https://cdn.mint.gg/a/model.webp"), ".webp")
        self.assertEqual(_thumbnail_extension("https://cdn.mint.gg/a/model.png?x=1"), ".png")
        self.assertEqual(_thumbnail_extension("https://cdn.mint.gg/a/model"), ".img")

    def test_draw_world_tab_explains_kiri_requirement_when_unavailable(self):
        state = AssetBrowserState(
            status="Loaded",
            active_tab="worlds",
            worlds=[
                {
                    "id": "world_1",
                    "name": "Gallery",
                    "status": "succeeded",
                    "importSupported": False,
                }
            ],
        )
        layout = FakeLayout()

        draw_asset_browser(layout, state)

        self.assertIn("Kiri required", layout.labels)
        self.assertNotIn(
            "mint_asset_browser.import_model",
            [call.operator_id for call in layout.operators],
        )
        self.assertNotIn(
            "mint_asset_browser.import_world_kiri",
            [call.operator_id for call in layout.operators],
        )
        kiri_calls = [
            call for call in layout.operators
            if call.operator_id == "mint_asset_browser.open_in_mint"
            and call.text == "Get Kiri"
        ]
        self.assertEqual(len(kiri_calls), 1)
        self.assertEqual(kiri_calls[0].url, KIRI_DOWNLOAD_URL)

    def test_draw_world_asset_exposes_kiri_import_when_available(self):
        layout = FakeLayout()

        _draw_asset(
            layout,
            {
                "id": "world_1",
                "name": "Gallery",
                "status": "succeeded",
            },
            is_model=False,
            world_import_available=True,
        )

        import_calls = [
            call for call in layout.operators
            if call.operator_id == "mint_asset_browser.import_world_kiri"
        ]
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(import_calls[0].text, "View Splat")
        self.assertEqual(import_calls[0].asset_id, "world_1")

    def test_draw_world_asset_shows_active_import_status_on_button(self):
        class ActiveJob:
            world = {"id": "world_1"}

            def snapshot(self):
                return False, "Downloading...", None, None

        layout = FakeLayout()
        ui_module._WORLD_SPLAT_IMPORT_JOB = ActiveJob()

        _draw_asset(
            layout,
            {
                "id": "world_1",
                "name": "Gallery",
                "status": "succeeded",
            },
            is_model=False,
            world_import_available=True,
        )

        import_calls = [
            call for call in layout.operators
            if call.operator_id == "mint_asset_browser.import_world_kiri"
        ]
        self.assertEqual(import_calls[0].text, "Downloading...")
        self.assertEqual(import_calls[0].asset_id, "world_1")

    def test_draw_active_splat_panel_explains_kiri_requirement(self):
        layout = FakeLayout()

        draw_active_splat_panel(layout, FakeKiriBpy(with_operator=False))

        self.assertIn("Kiri unavailable", layout.labels)
        kiri_calls = [
            call for call in layout.operators
            if call.operator_id == "mint_asset_browser.open_in_mint"
            and call.text == "Get Kiri"
        ]
        self.assertEqual(len(kiri_calls), 1)
        self.assertEqual(kiri_calls[0].url, KIRI_DOWNLOAD_URL)

    def test_draw_active_splat_panel_handles_empty_scene(self):
        layout = FakeLayout()

        draw_active_splat_panel(layout, FakeKiriBpy())

        self.assertIn("No splats in scene", layout.labels)

    def test_draw_active_splat_panel_shows_active_splat_controls(self):
        active = FakeSplat("Gallery", update_mode="Show As Point Cloud")
        inactive = FakeSplat("Street")
        fake_bpy = FakeKiriBpy([inactive, active])
        fake_bpy.context.view_layer.objects.active = active
        layout = FakeLayout()

        draw_active_splat_panel(layout, fake_bpy)

        self.assertIn("Active: Gallery", layout.labels)
        self.assertIn("Street", layout.labels)
        self.assertEqual(
            [call.operator_id for call in layout.operators],
            [
                "mint_asset_browser.set_splat_mode",
                "mint_asset_browser.activate_splat",
                "mint_asset_browser.fine_splat_points",
                "mint_asset_browser.update_splat_view",
                "mint_asset_browser.toggle_splat_auto_update",
                "mint_asset_browser.activate_splat",
            ],
        )
        self.assertEqual(layout.operators[0].object_name, "Gallery")
        self.assertEqual(layout.operators[0].text, "Render Mode")
        self.assertEqual(layout.operators[0].mode, "CAMERA")
        self.assertEqual(layout.operators[1].object_name, "Gallery")
        self.assertEqual(layout.operators[1].text, "Point Cloud")
        self.assertEqual(layout.operators[2].object_name, "Gallery")
        self.assertEqual(layout.operators[2].text, "Finer")
        self.assertEqual(layout.operators[3].object_name, "Gallery")
        self.assertEqual(layout.operators[3].text, "Update View")
        self.assertEqual(layout.operators[4].object_name, "Gallery")
        self.assertEqual(layout.operators[4].text, "Auto Update")
        self.assertEqual(layout.operators[5].object_name, "Street")
        self.assertEqual(layout.operators[5].text, "Edit")
        self.assertEqual(layout.operator_menus, [])
        self.assertEqual(layout.props[0][1], '["Socket_51"]')
        self.assertEqual(layout.props[0][2], "Point Size")

    def test_draw_active_splat_panel_shows_render_and_auto_update_buttons(self):
        active = FakeSplat("Gallery", update_mode="Enable Camera Updates")
        fake_bpy = FakeKiriBpy([active])
        fake_bpy.context.view_layer.objects.active = active
        layout = FakeLayout()

        draw_active_splat_panel(layout, fake_bpy)

        self.assertEqual(layout.operators[0].text, "Render Mode")
        self.assertEqual(layout.operators[0].mode, "CAMERA")
        self.assertEqual(layout.operators[1].text, "Point Cloud")
        self.assertEqual(layout.operators[2].text, "Update View")
        self.assertEqual(layout.operators[3].text, "Auto Update")
        self.assertEqual(layout.operator_menus, [])
        self.assertEqual(layout.props, [])
        self.assertEqual(ui_module._ACTIVE_SPLAT_AUTO_UPDATE_INTERVAL_SECONDS, 3.0)

    def test_draw_asset_browser_places_active_splat_between_account_and_assets(self):
        state = AssetBrowserState(
            status="Loaded",
            active_tab="worlds",
            worlds=[
                {
                    "id": "world_1",
                    "name": "Gallery",
                    "status": "succeeded",
                    "importSupported": False,
                }
            ],
        )
        active = FakeSplat("Gallery", update_mode="Enable Camera Updates")
        fake_bpy = FakeKiriBpy([active])
        fake_bpy.context.view_layer.objects.active = active
        layout = FakeLayout()
        original_bpy = ui_module.bpy
        ui_module.bpy = fake_bpy
        try:
            draw_asset_browser(layout, state)
        finally:
            ui_module.bpy = original_bpy

        events = layout.events
        account_index = events.index(("operator", "My Account"))
        active_index = events.index(("operator", "Active Splat"))
        assets_index = events.index(("operator", "My Mint Assets"))
        self.assertLess(account_index, active_index)
        self.assertLess(active_index, assets_index)
        self.assertIn("Active: Gallery", layout.labels)

    def test_world_splat_poll_waits_for_background_prepare_job(self):
        class PendingJob:
            log_path = Path("/tmp/kiri.log")

            def snapshot(self):
                return False, "Requesting Mint PLY export", None, None

        ui_module._WORLD_SPLAT_IMPORT_JOB = PendingJob()
        ui_module.RUNTIME_STATE.status = "Before"

        self.assertEqual(ui_module._poll_world_splat_import_job(), 0.5)
        self.assertEqual(ui_module.RUNTIME_STATE.status, "Before")
        self.assertIsNotNone(ui_module._WORLD_SPLAT_IMPORT_JOB)

    def test_world_splat_poll_imports_prepared_ply_and_clears_job(self):
        imported = []

        class DoneJob:
            log_path = Path("/tmp/kiri.log")
            world = {"id": "world_1", "name": "Gallery"}
            config = MintBlenderConfig()

            def snapshot(self):
                return True, "Importing...", Path("/tmp/world.ply"), None

            def _set_status(self, status):
                self.status = status

        original_import = ui_module.import_prepared_world_splat_with_kiri
        ui_module.import_prepared_world_splat_with_kiri = (
            lambda path, world, **kwargs: imported.append((path, world, kwargs))
        )
        try:
            ui_module._WORLD_SPLAT_IMPORT_JOB = DoneJob()
            ui_module.RUNTIME_STATE.status = "Loaded"

            self.assertIsNone(ui_module._poll_world_splat_import_job())
        finally:
            ui_module.import_prepared_world_splat_with_kiri = original_import

        self.assertEqual(imported[0][0], Path("/tmp/world.ply"))
        self.assertEqual(imported[0][1]["id"], "world_1")
        self.assertEqual(ui_module.RUNTIME_STATE.status, "Loaded")
        self.assertIsNone(ui_module._WORLD_SPLAT_IMPORT_JOB)

    def test_preview_lines_name_asset_collection_and_location(self):
        lines = ui_module.preview_lines(
            "Gallery Crate",
            {"collection": "Props"},
            "File origin (glTF coordinates)",
        )

        self.assertEqual(
            lines,
            [
                "Import: Gallery Crate",
                "Into collection: Props",
                "Location: File origin (glTF coordinates)",
            ],
        )

    def test_world_splat_poll_refuses_import_when_scene_changed_since_preview(self):
        imported = []

        class Cursor:
            location = [0.0, 0.0, 0.0]

        class Scene:
            name = "Scene"
            cursor = Cursor()
            collection = None

        class Context:
            scene = Scene()
            view_layer = None

        class FakeBpy:
            context = Context()
            data = None

        fake_bpy = FakeBpy()
        preview = ui_module.import_preview(fake_bpy)
        Cursor.location = [3.0, 0.0, 0.0]

        class DoneJob:
            log_path = Path("/tmp/kiri.log")
            world = {"id": "world_1", "name": "Gallery"}
            config = MintBlenderConfig()

            def __init__(self):
                self.preview = preview

            def snapshot(self):
                return True, "Importing...", Path("/tmp/world.ply"), None

            def _set_status(self, status):
                self.status = status

        original_import = ui_module.import_prepared_world_splat_with_kiri
        original_bpy = ui_module.bpy
        ui_module.import_prepared_world_splat_with_kiri = (
            lambda path, world, **kwargs: imported.append(path)
        )
        ui_module.bpy = fake_bpy
        try:
            ui_module._WORLD_SPLAT_IMPORT_JOB = DoneJob()
            self.assertIsNone(ui_module._poll_world_splat_import_job())
        finally:
            ui_module.import_prepared_world_splat_with_kiri = original_import
            ui_module.bpy = original_bpy

        self.assertEqual(imported, [])
        self.assertEqual(
            ui_module.RUNTIME_STATE.status,
            "Scene changed since preview, preview again",
        )
        self.assertIsNone(ui_module._WORLD_SPLAT_IMPORT_JOB)

    def test_draw_packs_tab_drills_into_child_models(self):
        state = AssetBrowserState(
            status="Loaded",
            active_tab="packs",
            packs=[
                {
                    "id": "pack_1",
                    "name": "Prop Kit",
                    "status": "succeeded",
                    "modelCount": 1,
                    "mintUrl": "https://mint.gg/0xAlice/pack_1",
                    "items": [
                        {
                            "id": "item_1",
                            "label": "Barrel",
                            "model": {
                                "id": "model_pack_1",
                                "name": "Barrel",
                                "status": "succeeded",
                                "importSupported": True,
                            },
                        },
                    ],
                }
            ],
        )
        layout = FakeLayout()

        draw_asset_browser(layout, state)

        operator_ids = [call.operator_id for call in layout.operators]
        self.assertIn("mint_asset_browser.show_packs", operator_ids)
        self.assertIn("mint_asset_browser.open_pack", operator_ids)
        self.assertIn("Prop Kit", layout.labels)
        self.assertIn("1 model", layout.labels)

        state.selected_pack_id = "pack_1"
        child_layout = FakeLayout()
        draw_asset_browser(child_layout, state)

        child_operator_ids = [call.operator_id for call in child_layout.operators]
        self.assertIn("mint_asset_browser.back_to_packs", child_operator_ids)
        self.assertIn("mint_asset_browser.import_model", child_operator_ids)
        self.assertIn("Barrel", child_layout.labels)

    def test_refresh_uses_stored_refresh_token_after_unauthorized_response(self):
        class FakeStore:
            def __init__(self):
                self.saved = []

            def load(self):
                return StoredRefreshToken(
                    refresh_token="refresh_123",
                    client_id="mint_blender_test",
                    token_url="https://mcp.test/oauth/token",
                )

            def save(self, token):
                self.saved.append(token)

        class FakeClient:
            def me(self, access_token):
                if access_token == "expired":
                    raise MintApiError(401, "Expired")
                return {"walletAddress": "0xAlice"}

            def list_models(self, access_token, limit=50):
                return {"data": [{"id": "model_1"}]}

            def list_worlds(self, access_token, limit=50):
                return {"data": []}

            def list_packs(self, access_token, limit=50):
                return {"data": []}

        original_exchange = ui_module.exchange_refresh_token
        ui_module.exchange_refresh_token = lambda config, refresh_token: TokenResponse(
            access_token="access_next",
            refresh_token="refresh_next",
            expires_in=3600,
            token_type="Bearer",
        )

        try:
            state = AssetBrowserState(access_token="expired")
            store = FakeStore()
            refresh_with_stored_token(
                MintBlenderConfig(
                    oauth_token_url="https://mcp.test/oauth/token",
                    client_id="mint_blender_test",
                ),
                state=state,
                token_store=store,
                client=FakeClient(),
            )
        finally:
            ui_module.exchange_refresh_token = original_exchange

        self.assertEqual(state.access_token, "access_next")
        self.assertEqual(state.status, "Loaded 1 models, 0 worlds, and 0 packs")
        self.assertEqual(store.saved[0].refresh_token, "refresh_next")

    def test_refresh_restores_session_from_refresh_token_without_access_token(self):
        class FakeStore:
            def load(self):
                return StoredRefreshToken(
                    refresh_token="refresh_123",
                    client_id="mint_blender_test",
                    token_url="https://mcp.test/oauth/token",
                )

            def save(self, token):
                pass

        class FakeClient:
            def me(self, access_token):
                if access_token != "access_next":
                    raise MintApiError(401, "Expired")
                return {"walletAddress": "0xAlice"}

            def list_models(self, access_token, limit=50):
                return {"data": [{"id": "model_1"}, {"id": "model_2"}]}

            def list_worlds(self, access_token, limit=50):
                return {"data": [{"id": "world_1"}]}

            def list_packs(self, access_token, limit=50):
                return {"data": []}

        original_exchange = ui_module.exchange_refresh_token
        ui_module.exchange_refresh_token = lambda config, refresh_token: TokenResponse(
            access_token="access_next",
            refresh_token=None,
            expires_in=3600,
            token_type="Bearer",
        )

        try:
            state = AssetBrowserState()
            refresh_with_stored_token(
                MintBlenderConfig(
                    oauth_token_url="https://mcp.test/oauth/token",
                    client_id="mint_blender_test",
                ),
                state=state,
                token_store=FakeStore(),
                client=FakeClient(),
            )
        finally:
            ui_module.exchange_refresh_token = original_exchange

        self.assertEqual(state.access_token, "access_next")
        self.assertEqual(state.status, "Loaded 2 models, 1 worlds, and 0 packs")

    def test_oauth_completion_refreshes_assets_immediately(self):
        class FakeSession:
            state = "state_123"
            verifier = "verifier_123"
            redirect_uri = "http://127.0.0.1:49152/oauth/callback"

        class FakeController:
            pending_session = FakeSession()

        class FakeCallback:
            closed = False

            def wait_for_callback(self, expected_state):
                self.expected_state = expected_state
                return "code_123"

            def close(self):
                self.closed = True

        class FakeStore:
            def __init__(self):
                self.saved = []

            def save(self, token):
                self.saved.append(token)

        class FakeClient:
            def __init__(self, config):
                self.config = config

            def me(self, access_token):
                return {"walletAddress": "0xAlice"}

            def list_models(self, access_token, limit=50):
                return {"data": [{"id": "model_1"}]}

            def list_worlds(self, access_token, limit=50):
                return {"data": [{"id": "world_1"}]}

            def list_packs(self, access_token, limit=50):
                return {"data": []}

        original_state = ui_module.RUNTIME_STATE
        original_exchange = ui_module.exchange_authorization_code
        original_client = ui_module.MintBlenderClient
        ui_module.RUNTIME_STATE = AssetBrowserState()
        ui_module.exchange_authorization_code = (
            lambda config, code, code_verifier, redirect_uri: TokenResponse(
                access_token="access_123",
                refresh_token="refresh_123",
                expires_in=3600,
                token_type="Bearer",
            )
        )
        ui_module.MintBlenderClient = FakeClient

        try:
            store = FakeStore()
            callback = FakeCallback()
            ui_module._complete_oauth(
                MintBlenderConfig(client_id="mint_blender_test"),
                store,
                callback,
                FakeController(),
            )

            self.assertEqual(callback.expected_state, "state_123")
            self.assertTrue(callback.closed)
            self.assertEqual(store.saved[0].refresh_token, "refresh_123")
            self.assertEqual(ui_module.RUNTIME_STATE.access_token, "access_123")
            self.assertEqual(
                ui_module.RUNTIME_STATE.status,
                "Loaded 1 models, 1 worlds, and 0 packs",
            )
        finally:
            ui_module.RUNTIME_STATE = original_state
            ui_module.exchange_authorization_code = original_exchange
            ui_module.MintBlenderClient = original_client


if __name__ == "__main__":
    unittest.main()
