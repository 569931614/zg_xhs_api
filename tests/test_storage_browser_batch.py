import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.services.storage import (
    model_image_data_url,
    optimize_image_for_upload,
    should_use_browser_batch_download,
)


class StorageBrowserBatchTest(unittest.TestCase):
    def test_goldwood_images_use_shared_browser_batch(self) -> None:
        images = [
            {"url": "https://goldwoodbyboris.com/media/catalog/product/a.jpg"},
            {"url": "https://www.goldwoodbyboris.com/media/catalog/product/b.jpg"},
        ]

        with patch.dict(os.environ, {}, clear=False):
            self.assertTrue(should_use_browser_batch_download(images))

    def test_other_hosts_keep_default_download_path(self) -> None:
        images = [{"url": "https://example.com/a.jpg"}]

        with patch.dict(os.environ, {}, clear=False):
            self.assertFalse(should_use_browser_batch_download(images))

    def test_browser_fallback_flag_disables_batch(self) -> None:
        images = [{"url": "https://goldwoodbyboris.com/media/catalog/product/a.jpg"}]

        with patch.dict(os.environ, {"IMAGE_DOWNLOAD_BROWSER_FALLBACK": "false"}, clear=False):
            self.assertFalse(should_use_browser_batch_download(images))

    def test_non_jpeg_images_are_normalized_for_model_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "image.webp"
            Image.new("RGB", (20, 20), "white").save(source, "WEBP")

            normalized, _ = optimize_image_for_upload(source, source.stat().st_size)

            self.assertEqual(normalized.suffix, ".jpg")
            self.assertTrue(model_image_data_url(normalized).startswith("data:image/jpeg;base64,"))


if __name__ == "__main__":
    unittest.main()
