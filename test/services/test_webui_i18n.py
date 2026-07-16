import ast
import json
from pathlib import Path
import unittest


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
WEBUI_COMPONENTS = ROOT_DIR / "webui" / "components"
I18N_DIR = ROOT_DIR / "webui" / "i18n"

SPANISH_SHARED_STATIC_VALUES = {
    "Chatterbox Base URL Placeholder",
    "Chatterbox Voices Placeholder",
    "Coverr",
    "FadeIn",
    "FadeOut",
    "Pexels",
    "Pixabay",
    "Publishing Hashtags",
    "Scanner",
    "SlideIn",
    "SlideOut",
    "Stitch",
    "Subtitle Background Color",
}


class _TrKeyVisitor(ast.NodeVisitor):
    def __init__(self):
        self.keys = set()

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "tr"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self.keys.add(node.args[0].value)
        self.generic_visit(node)


def _webui_sources():
    sources = [WEBUI_MAIN]
    if WEBUI_COMPONENTS.exists():
        sources.extend(sorted(WEBUI_COMPONENTS.rglob("*.py")))
    return sources


def _static_translation_keys():
    visitor = _TrKeyVisitor()
    for source in _webui_sources():
        visitor.visit(ast.parse(source.read_text(encoding="utf-8")))
    return visitor.keys


def _load_locale(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_translation(locale):
    return _load_locale(I18N_DIR / f"{locale}.json").get("Translation", {})


class TestWebuiI18n(unittest.TestCase):
    def test_custom_script_requirements_accept_browser_paste(self):
        inputs = []
        for source in _webui_sources():
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function = node.func
                if not isinstance(function, ast.Attribute) or function.attr != "text_area":
                    continue
                label = node.args[0]
                if (
                    isinstance(label, ast.Call)
                    and isinstance(label.func, ast.Name)
                    and label.func.id == "tr"
                    and label.args
                    and isinstance(label.args[0], ast.Constant)
                    and label.args[0].value == "Custom Script Requirements"
                ):
                    inputs.append(node)

        self.assertEqual(len(inputs), 2)
        for node in inputs:
            self.assertNotIn("max_chars", {keyword.arg for keyword in node.keywords})

    def test_locale_json_documents_are_valid(self):
        for path in sorted(I18N_DIR.glob("*.json")):
            with self.subTest(locale=path.stem):
                data = _load_locale(path)
                self.assertIsInstance(data.get("Language"), str)
                self.assertTrue(data["Language"].strip())
                self.assertIsInstance(data.get("Translation"), dict)
                self.assertTrue(data["Translation"])
                self.assertTrue(
                    all(
                        isinstance(key, str)
                        and isinstance(value, str)
                        and value.strip()
                        for key, value in data["Translation"].items()
                    )
                )

    def test_every_locale_has_complete_key_set(self):
        expected_keys = set(_load_translation("en"))
        for path in sorted(I18N_DIR.glob("*.json")):
            with self.subTest(locale=path.stem):
                self.assertEqual(set(_load_translation(path.stem)), expected_keys)

    def test_english_spanish_and_russian_cover_static_webui_labels(self):
        static_keys = _static_translation_keys()
        for locale in ("en", "es", "ru"):
            with self.subTest(locale=locale):
                self.assertEqual(
                    sorted(static_keys - set(_load_translation(locale))), []
                )

    def test_spanish_static_labels_do_not_use_english_fallbacks(self):
        static_keys = _static_translation_keys()
        english = _load_translation("en")
        spanish = _load_translation("es")
        untranslated = {
            key
            for key in static_keys
            if spanish[key] == english[key]
            and key not in SPANISH_SHARED_STATIC_VALUES
        }

        self.assertEqual(sorted(untranslated), [])

    def test_script_language_options_include_russian(self):
        tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        support_locales = None

        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "support_locales"
                for target in node.targets
            ):
                support_locales = ast.literal_eval(node.value)
                break

        self.assertIsNotNone(support_locales)
        self.assertIn("ru-RU", support_locales)
