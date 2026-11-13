import tempfile
import unittest
from pathlib import Path

from PIL import Image

from image_prepare import prepare_instagram_image


class ImagePrepareTest(unittest.TestCase):
    def test_portrait_is_cropped_to_four_fifths(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.jpg"
            target = Path(tmp) / "target.jpg"
            Image.new("RGB", (800, 1200), "white").save(source)
            prepare_instagram_image(source, target)
            with Image.open(target) as result:
                self.assertAlmostEqual(result.width / result.height, 0.8, places=2)
                self.assertEqual(result.format, "JPEG")


if __name__ == "__main__":
    unittest.main()
