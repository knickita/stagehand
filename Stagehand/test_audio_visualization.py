import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class AudioVisualizationTests(unittest.TestCase):
    def test_vio_catalogue_entry_has_complete_audio_configuration(self):
        catalogue = json.loads((ROOT / "Catalogue.json").read_text(encoding="utf-8"))
        vio = next(item for item in catalogue["items"] if item["uniqueId"] == 1)

        self.assertIn("audioarray", vio["tags"])
        self.assertIn("audiosource", vio["tags"])
        self.assertEqual(
            {
                "splAtOneMeter",
                "delayMeters",
                "reversePolarity",
                "muted",
                "attenuation",
                "omnidirectional",
                "horizontalDispersion",
                "verticalDispersion",
                "nearDispersionPlaneDistance",
                "dispersionPlaneSize",
                "sourceOffset",
            },
            set(vio["audio"]),
        )
        self.assertGreater(vio["audio"]["splAtOneMeter"], 0.0)
        self.assertEqual(2, len(vio["audio"]["dispersionPlaneSize"]))
        self.assertEqual([0.0, 0.0, -0.127], vio["audio"]["sourceOffset"])

    def test_vio_s218_is_an_unconnectable_omnidirectional_source(self):
        catalogue = json.loads((ROOT / "Catalogue.json").read_text(encoding="utf-8"))
        sub = next(item for item in catalogue["items"] if item["uniqueId"] == 68)

        self.assertEqual("vio S218", sub["name"])
        self.assertEqual("Models/Audio/vio_s218", sub["mesh3d"])
        self.assertEqual(["audiosource"], sub["tags"])
        self.assertEqual([], sub["links"])
        self.assertTrue(sub["audio"]["omnidirectional"])
        self.assertEqual(143.0, sub["audio"]["splAtOneMeter"])

    def test_audio_module_defines_overlay_operator_and_pressure_math(self):
        source = (ROOT / "AudioVisualization.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        class_names = {
            node.name for node in module.body if isinstance(node, ast.ClassDef)
        }

        self.assertIn("STAGEHAND_OT_toggle_audio_visualization", class_names)
        self.assertIn("SpaceView3D.draw_handler_add", source)
        self.assertIn("GPUShaderCreateInfo", source)
        self.assertIn("GPUUniformBuf", source)
        self.assertIn("GPUTexture", source)
        self.assertIn("texelFetch", source)
        self.assertNotIn("gpu.types.GPUShader(", source)
        self.assertNotIn("MAX_AUDIO_SOURCES", source)
        self.assertNotIn("[:MAX_AUDIO_SOURCES]", source)
        self.assertIn(
            "sourceIndex < int(audioData.sourceCount)",
            source,
        )
        self.assertIn("max_texture_size_get", source)
        self.assertIn("def audio_source_counts(context):", source)
        self.assertIn('AUDIO_SOURCE_TAG = "audiosource"', source)
        self.assertIn("Vector((0.0, -1.0, 0.0))", source)
        self.assertIn(
            "matrix_world @ Vector(stagehand.audioSourceOffset)",
            source,
        )
        self.assertIn("actualSpl = positionSpl.w - 20.0", source)
        self.assertIn("distanceMeters + audio.x", source)
        self.assertIn("pressureReal * pressureReal", source)
        self.assertIn("directivityFactor", source)
        self.assertIn("clipPosition.z -= 0.00001 * clipPosition.w", source)
        self.assertIn("if _has_audio_source_tag(obj):", source)

    def test_addon_registers_audio_module_before_the_ui(self):
        source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('AudioVisualization = _load_submodule("AudioVisualization")', source)
        self.assertLess(
            source.index("    AudioVisualization,"),
            source.index("    MenuConfiguration,"),
        )

    def test_every_audio_property_supports_selected_source_editing(self):
        source = (ROOT / "AddStagehandObject.py").read_text(encoding="utf-8")
        audio_properties = (
            "audioMuted",
            "audioSplAtOneMeter",
            "audioAttenuation",
            "audioDelayMeters",
            "audioReversePolarity",
            "audioOmnidirectional",
            "audioHorizontalDispersion",
            "audioVerticalDispersion",
            "audioNearDispersionDistance",
            "audioDispersionPlaneSize",
            "audioSourceOffset",
        )

        for property_name in audio_properties:
            self.assertIn(
                f'update=_make_audio_multi_edit_update("{property_name}")',
                source,
            )
        self.assertIn("_audio_multi_edit_suppression", source)
        self.assertIn("Editing {selected_audio_count} selected sources", source)

    def test_audio_delay_and_attenuation_have_safe_ranges(self):
        source = (ROOT / "AddStagehandObject.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        stagehand_class = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "StagehandObject"
        )
        annotated_properties = {
            node.target.id: node.annotation
            for node in stagehand_class.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
        }

        delay_keywords = {
            keyword.arg: ast.literal_eval(keyword.value)
            for keyword in annotated_properties["audioDelayMeters"].keywords
            if keyword.arg in {"min", "max"}
        }
        attenuation_keywords = {
            keyword.arg: ast.literal_eval(keyword.value)
            for keyword in annotated_properties["audioAttenuation"].keywords
            if keyword.arg in {"min", "max"}
        }
        self.assertEqual(0.0, delay_keywords["min"])
        self.assertEqual(-120.0, attenuation_keywords["min"])
        self.assertEqual(0.0, attenuation_keywords["max"])


if __name__ == "__main__":
    unittest.main()
