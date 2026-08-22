import os
import unittest
from unittest.mock import patch

from app.services.storage import should_use_browser_batch_download


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


if __name__ == "__main__":
    unittest.main()
