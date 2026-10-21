import tempfile
import unittest
from pathlib import Path

from mint_blender_asset_browser.cache import AssetCache
from mint_blender_asset_browser.importer import (
    SCENE_CHANGED_MESSAGE,
    ImportUnsupportedError,
    SceneChangedError,
    check_import_preview,
    import_model_asset,
    import_preview,
)


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body


class FakeNamed:
    def __init__(self, name, materials=None):
        self.name = name
        self.materials = materials or []


class FakeMaterialSlot:
    def __init__(self, material):
        self.material = material


class FakeObject:
    def __init__(self, name, data=None, materials=None):
        self.name = name
        self.data = data
        self.material_slots = [FakeMaterialSlot(material) for material in materials or []]


class FakeScene:
    def __init__(self, objects=None):
        self.objects = list(objects or [])


class FakeContext:
    def __init__(self, objects=None):
        self.scene = FakeScene(objects)
        self.selected_objects = []


class FakeBpy:
    def __init__(self, imported_objects=None, existing_objects=None):
        self.imports = []
        self.imported_objects = list(imported_objects or [])
        self.context = FakeContext(existing_objects)
        self.ops = self
        self.import_scene = self

    def gltf(self, filepath):
        self.imports.append(filepath)
        self.context.scene.objects.extend(self.imported_objects)
        self.context.selected_objects = list(self.imported_objects)


class FakeDataCollection(list):
    def remove(self, item):
        list.remove(self, item)

    def __contains__(self, name):
        return any(getattr(item, "name", None) == name for item in self)


class FakeCursor:
    def __init__(self, location):
        self.location = list(location)


class FakeCollection:
    def __init__(self, name, objects=None):
        self.name = name
        self.objects = list(objects or [])


class FakeLayerCollection:
    def __init__(self, collection):
        self.collection = collection


class FakeViewLayer:
    def __init__(self, collection):
        self.active_layer_collection = FakeLayerCollection(collection)


class FakeData:
    def __init__(self):
        self.objects = FakeDataCollection()
        self.meshes = FakeDataCollection()
        self.collections = FakeDataCollection()
        self.is_dirty = False


class FakeSceneBpy(FakeBpy):
    """Fake bpy with a target collection, 3D cursor and bpy.data."""

    def __init__(self, imported_objects=None, fail_import=False):
        super().__init__(imported_objects=imported_objects)
        self.fail_import = fail_import
        self.data = FakeData()
        self.target = FakeCollection("Props")
        self.data.collections.append(self.target)
        self.context.scene.name = "Scene"
        self.context.scene.cursor = FakeCursor((0.0, 0.0, 0.0))
        self.context.scene.collection = FakeCollection("Scene Collection")
        self.context.view_layer = FakeViewLayer(self.target)

    def gltf(self, filepath):
        super().gltf(filepath)
        for obj in self.imported_objects:
            self.data.objects.append(obj)
            self.target.objects.append(obj)
            if obj.data is not None:
                self.data.meshes.append(obj.data)
        if self.fail_import:
            raise RuntimeError("glTF importer failed")


MODEL = {
    "id": "model_1",
    "name": "Crate",
    "importSupported": True,
    "importUrl": "https://cdn.mint.gg/model.glb",
}


class ImporterTest(unittest.TestCase):
    def test_import_model_downloads_cached_glb_and_dispatches_blender_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = AssetCache(Path(tmp))
            fake_bpy = FakeBpy()
            requested = []

            def urlopen(request, **kwargs):
                requested.append((request, kwargs))
                return FakeResponse(b"glb-bytes")

            path = import_model_asset(
                {
                    "id": "model_1",
                    "importSupported": True,
                    "importUrl": "https://cdn.mint.gg/model.glb",
                },
                cache,
                urlopen=urlopen,
                bpy_module=fake_bpy,
            )

            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"glb-bytes")
            self.assertEqual([request.full_url for request, _kwargs in requested], ["https://cdn.mint.gg/model.glb"])
            self.assertEqual(requested[0][1]["timeout"], 30)
            self.assertEqual(requested[0][0].get_header("User-agent"), "MintBlenderAssetBrowser/0.1.0")
            self.assertEqual(
                requested[0][0].get_header("Accept"),
                "model/gltf-binary, application/octet-stream, */*",
            )
            self.assertEqual(fake_bpy.imports, [str(path)])

    def test_import_model_reuses_cached_glb(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = AssetCache(Path(tmp))
            fake_bpy = FakeBpy()
            cache.write_bytes("model_1", "https://cdn.mint.gg/model.glb", b"cached")

            path = import_model_asset(
                {
                    "id": "model_1",
                    "importSupported": True,
                    "importUrl": "https://cdn.mint.gg/model.glb",
                },
                cache,
                urlopen=lambda url: self.fail("cache hit should not download"),
                bpy_module=fake_bpy,
            )

            self.assertEqual(path.read_bytes(), b"cached")
            self.assertEqual(fake_bpy.imports, [str(path)])

    def test_import_model_renames_imported_objects_with_mint_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = AssetCache(Path(tmp))
            material = FakeNamed("tripo_material_5bbc56f3")
            mesh = FakeNamed("tripo_mesh_5bbc56f3", materials=[material])
            imported = FakeObject("tripo_node_5bbc56f3", data=mesh, materials=[material])
            fake_bpy = FakeBpy(imported_objects=[imported])

            import_model_asset(
                {
                    "id": "model_1",
                    "name": "  Heavy   Gunner  ",
                    "importSupported": True,
                    "importUrl": "https://cdn.mint.gg/model.glb",
                },
                cache,
                urlopen=lambda request, **kwargs: FakeResponse(b"glb-bytes"),
                bpy_module=fake_bpy,
            )

            self.assertEqual(imported.name, "Heavy Gunner")
            self.assertEqual(mesh.name, "Heavy Gunner Mesh")
            self.assertEqual(material.name, "Heavy Gunner Material")

    def test_import_model_suffixes_multiple_imported_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = AssetCache(Path(tmp))
            first_mesh = FakeNamed("tripo_mesh_first")
            second_mesh = FakeNamed("tripo_mesh_second")
            first = FakeObject("tripo_node_first", data=first_mesh)
            second = FakeObject("tripo_node_second", data=second_mesh)
            fake_bpy = FakeBpy(imported_objects=[first, second])

            import_model_asset(
                {
                    "id": "model_1",
                    "name": "Scout Pilot",
                    "importSupported": True,
                    "importUrl": "https://cdn.mint.gg/model.glb",
                },
                cache,
                urlopen=lambda request, **kwargs: FakeResponse(b"glb-bytes"),
                bpy_module=fake_bpy,
            )

            self.assertEqual(first.name, "Scout Pilot 01")
            self.assertEqual(second.name, "Scout Pilot 02")
            self.assertEqual(first_mesh.name, "Scout Pilot Mesh 01")
            self.assertEqual(second_mesh.name, "Scout Pilot Mesh 02")

    def test_import_model_rejects_non_importable_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ImportUnsupportedError):
                import_model_asset(
                    {
                        "id": "model_raw",
                        "importSupported": False,
                        "importUrl": None,
                    },
                    AssetCache(Path(tmp)),
                    bpy_module=FakeBpy(),
                )


class ImportPreviewTest(unittest.TestCase):
    def test_preview_names_target_collection_cursor_and_counts(self):
        fake_bpy = FakeSceneBpy()

        preview = import_preview(fake_bpy)

        self.assertEqual(preview["scene"], "Scene")
        self.assertEqual(preview["collection"], "Props")
        self.assertTrue(preview["collectionExists"])
        self.assertEqual(preview["collectionObjectCount"], 0)
        self.assertEqual(preview["cursor"], [0.0, 0.0, 0.0])
        self.assertFalse(preview["isDirty"])

    def test_preview_compare_passes_for_unchanged_scene(self):
        fake_bpy = FakeSceneBpy()
        preview = import_preview(fake_bpy)

        check_import_preview(preview, fake_bpy)

    def test_preview_compare_detects_each_scene_change(self):
        changes = [
            lambda bpy_ref: setattr(bpy_ref.context.scene.cursor, "location", [1.0, 0.0, 0.0]),
            lambda bpy_ref: setattr(bpy_ref.context.scene, "name", "Other"),
            lambda bpy_ref: bpy_ref.target.objects.append(FakeObject("Cube")),
            lambda bpy_ref: setattr(bpy_ref.data, "is_dirty", True),
            lambda bpy_ref: bpy_ref.data.collections.remove(bpy_ref.target),
            lambda bpy_ref: setattr(
                bpy_ref.context.view_layer.active_layer_collection,
                "collection",
                FakeCollection("Other"),
            ),
        ]
        for change in changes:
            fake_bpy = FakeSceneBpy()
            preview = import_preview(fake_bpy)
            change(fake_bpy)
            with self.assertRaises(SceneChangedError) as raised:
                check_import_preview(preview, fake_bpy)
            self.assertEqual(str(raised.exception), SCENE_CHANGED_MESSAGE)

    def test_import_with_matching_preview_imports_into_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            imported = FakeObject("node", data=FakeNamed("mesh"))
            fake_bpy = FakeSceneBpy(imported_objects=[imported])
            preview = import_preview(fake_bpy)

            path = import_model_asset(
                MODEL,
                AssetCache(Path(tmp)),
                urlopen=lambda request, **kwargs: FakeResponse(b"glb-bytes"),
                bpy_module=fake_bpy,
                preview=preview,
            )

            self.assertEqual(fake_bpy.imports, [str(path)])
            self.assertEqual(fake_bpy.target.objects, [imported])
            self.assertEqual(imported.name, "Crate")

    def test_import_refuses_when_scene_changed_since_preview_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bpy = FakeSceneBpy(imported_objects=[FakeObject("node", data=FakeNamed("mesh"))])
            preview = import_preview(fake_bpy)
            fake_bpy.context.scene.cursor.location = [2.0, 0.0, 0.0]

            with self.assertRaises(SceneChangedError):
                import_model_asset(
                    MODEL,
                    AssetCache(Path(tmp)),
                    urlopen=lambda request, **kwargs: FakeResponse(b"glb-bytes"),
                    bpy_module=fake_bpy,
                    preview=preview,
                )

            self.assertEqual(fake_bpy.imports, [])
            self.assertEqual(fake_bpy.target.objects, [])
            self.assertEqual(list(fake_bpy.data.objects), [])

    def test_failed_import_removes_only_datablocks_it_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = FakeObject("Existing", data=FakeNamed("Existing Mesh"))
            fake_bpy = FakeSceneBpy(
                imported_objects=[FakeObject("node", data=FakeNamed("mesh"))],
                fail_import=True,
            )
            fake_bpy.data.objects.append(existing)
            fake_bpy.data.meshes.append(existing.data)

            with self.assertRaises(RuntimeError):
                import_model_asset(
                    MODEL,
                    AssetCache(Path(tmp)),
                    urlopen=lambda request, **kwargs: FakeResponse(b"glb-bytes"),
                    bpy_module=fake_bpy,
                    preview=import_preview(fake_bpy),
                )

            self.assertEqual(list(fake_bpy.data.objects), [existing])
            self.assertEqual(list(fake_bpy.data.meshes), [existing.data])
            self.assertEqual(list(fake_bpy.data.collections), [fake_bpy.target])


if __name__ == "__main__":
    unittest.main()
