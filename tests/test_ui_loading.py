import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LoadingUiTest(unittest.TestCase):
    def test_base_contains_global_loading_dialog(self):
        template = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn("data-global-loading", template)
        self.assertIn("data-global-loading-message", template)
        self.assertIn("data-global-cancel", template)

    def test_javascript_preserves_publish_cancel_inside_overlay(self):
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("showGlobalLoading", script)
        self.assertIn("updateGlobalProgress", script)
        self.assertIn('a.account-tab[href]', script)
        self.assertIn("アカウントを切り替えています", script)
        self.assertIn('globalCancel.addEventListener("click", cancelPublish)', script)

    def test_styles_include_click_feedback_and_screen_lock(self):
        stylesheet = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn(".button.is-pressed", stylesheet)
        self.assertIn(".account-tab.is-pressed", stylesheet)
        self.assertIn(".global-loading", stylesheet)
        self.assertIn("body.is-loading", stylesheet)


if __name__ == "__main__":
    unittest.main()
