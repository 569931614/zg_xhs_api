import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.main import relay_scrape_url, should_relay_scrape
from app.schemas import ScrapeRequest


class ScrapeRelayTest(unittest.TestCase):
    def test_relay_disabled_by_default(self) -> None:
        self.assertFalse(should_relay_scrape("https://studio-1.nl/lighting/table-lamp/example"))
        self.assertFalse(should_relay_scrape("https://www.studio-1.nl/lighting/table-lamp/example"))

    def test_configured_domain_uses_relay(self) -> None:
        with patch("app.main.DEFAULT_SCRAPE_RELAY_DOMAINS", {"studio-1.nl", "www.studio-1.nl"}):
            self.assertTrue(should_relay_scrape("https://studio-1.nl/lighting/table-lamp/example"))
            self.assertTrue(should_relay_scrape("https://www.studio-1.nl/lighting/table-lamp/example"))

    def test_relay_header_prevents_recursive_relay(self) -> None:
        self.assertFalse(should_relay_scrape("https://studio-1.nl/lighting/table-lamp/example", relayed=True))

    def test_relay_scrape_posts_single_url_and_writes_job_result(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "name": "Kazuhide Takahama Nefer 1 Table Lamp",
            "price": "",
            "currency": "",
            "source_url": "https://studio-1.nl/lighting/table-lamp/example",
            "image_links": ["https://example.com/image.jpg"],
            "dimensions": "",
            "product_details": {"description": "A table lamp."},
        }
        payload = ScrapeRequest(
            url="https://studio-1.nl/lighting/table-lamp/example",
            render="auto",
            max_images=12,
            download_images=False,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("app.main.DATA_DIR", Path(tmp_dir)), patch("app.main.requests.post", return_value=response) as post:
                public_result, job_id, job_dir = relay_scrape_url(
                    "https://studio-1.nl/lighting/table-lamp/example",
                    payload,
                    "test-request",
                )

        post.assert_called_once()
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["url"], "https://studio-1.nl/lighting/table-lamp/example")
        self.assertEqual(kwargs["headers"]["X-Scrape-Relay"], "1")
        self.assertEqual(public_result["name"], "Kazuhide Takahama Nefer 1 Table Lamp")
        self.assertTrue(job_id)
        self.assertEqual(job_dir.name, job_id)


if __name__ == "__main__":
    unittest.main()
