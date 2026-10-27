import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from bs4 import BeautifulSoup
from PIL import Image

from app.services.extractor import extract, extract_domain_product_image_urls, extract_jsonld_products
from app.services.storage import ImageHostingError, download_image, image_response, public_image_cdn_url
from app.services.xhs_pipeline import download_image as download_xhs_image


PRODUCT_URL = "https://recondicionar.com.br/produtos/buffet/"
IMAGE_URL = "https://www.urbanmodern.nl/wp-content/uploads/2026/10/chair.jpg"


def product_html(products):
    return "".join(f'<script type="application/ld+json">{json.dumps(item)}</script>' for item in products)


class ProductSelectionTest(unittest.TestCase):
    def test_auctionet_uses_main_gallery_without_duplicate_thumbnails(self):
        for count in (1, 2, 4):
            for hostname in ("auctionet.com", "www.auctionet.com"):
                for limit in (1, 12):
                    with self.subTest(count=count, hostname=hostname, limit=limit):
                        page_url = f"https://{hostname}/da/5490527-aluminium-sculpture"
                        urls = [
                            f"https://images.auctionet.com/thumbs/large_item_5490527_{index:032x}.jpg"
                            for index in range(count)
                        ]
                        main = "".join(
                            f'<div class="item-images__main"><a href="/da/5490527/images#image_{index}">'
                            f'<img src="{url}"></a></div>'
                            for index, url in enumerate(urls)
                        )
                        thumbs = "".join(
                            f'<div class="item-images__thumb"><img src="{url.replace("large_item_", "mini_item_")}"></div>'
                            for url in urls
                        )
                        html = (
                            '<html><head><title>Aluminium sculpture</title>'
                            f'<meta property="og:image" content="{urls[0]}"></head><body>'
                            '<h1>Aluminium sculpture</h1>'
                            '<img src="https://images.auctionet.com/uploads/mini_auction_company_example.png">'
                            f'<div class="item-images">{main}{main}{thumbs}</div>'
                            '<div class="related"><img src="https://images.auctionet.com/thumbs/large_item_1234_other.jpg"></div>'
                            '</body></html>'
                        )
                        with patch("app.services.extractor.fetch_static", return_value=(html, page_url)):
                            result = extract(page_url, False, limit)
                        self.assertEqual([image["url"] for image in result["images"]], urls[:limit])

    def test_auctionet_missing_gallery_keeps_metadata_fallback(self):
        page_url = "https://auctionet.com/da/5490527-aluminium-sculpture"
        image_url = "https://images.auctionet.com/thumbs/large_item_5490527_image.jpg"
        html = (
            f'<html><head><meta property="og:image" content="{image_url}"></head>'
            '<body><h1>Aluminium sculpture</h1></body></html>'
        )
        with patch("app.services.extractor.fetch_static", return_value=(html, page_url)):
            result = extract(page_url, False, 12)
        self.assertEqual([image["url"] for image in result["images"]], [image_url])

    def test_kaderdesign_primary_gallery_excludes_same_artist_recommendations(self):
        page_url = "https://www.kaderdesign.com/available/p/1975-1990-abstract-sculpture-nr5"
        cdn = "https://images.squarespace-cdn.com/content/v1/site"
        sequence = [1, 7, 8, 2, 3, 4, 11, 5, 10, 6, 9, 10]
        urls = [
            f"{cdn}/current-{index}/Abstract-Sculpture-by-Cor-Dam-%235_{index:02d}.jpg"
            for index in sequence
        ]
        expected = list(dict.fromkeys(urls))
        gallery = ""
        for kind in ("thumbnails", "slides"):
            gallery += f'<div class="product-gallery-{kind}">'
            gallery += "".join(
                f'<img class="product-gallery-{kind}-item-image" data-src="{url}" '
                'data-image-dimensions="1782x2669">'
                for url in urls
            )
            gallery += "</div>"
        related = "".join(
            f'<img class="grid-item-image" src="{cdn}/related-{index}/'
            f'Abstract-Sculpture-by-Cor-Dam-%2336_{index:02d}.jpg" '
            'alt="Abstract Sculpture by Cor Dam" width="1782" height="2669">'
            for index in range(1, 15)
        )
        html = (
            '<html><head><title>Cor Dam Abstract Sculpture</title>'
            + product_html([{
                "@type": "Product",
                "url": page_url,
                "name": "Cor Dam Abstract Sculpture",
                "image": "http://static1.squarespace.com/static/site/Abstract-Sculpture-%235_01.jpg?format=1500w",
                "offers": {"price": "3500", "priceCurrency": "EUR"},
            }])
            + '</head><body><header><img src="/kader_logo.png"></header>'
            '<h1 class="product-title">Cor Dam Abstract Sculpture</h1>'
            f'<div class="product-gallery">{gallery}</div>'
            f'<div class="product-list"><div class="product-list-item">{related}</div></div>'
            '</body></html>'
        )
        for hostname in ("www.kaderdesign.com", "kaderdesign.com"):
            url = page_url.replace("www.kaderdesign.com", hostname)
            for limit in (12, 4):
                with self.subTest(hostname=hostname, limit=limit):
                    with patch("app.services.extractor.fetch_static", return_value=(html, url)):
                        result = extract(url, False, limit)
                    self.assertEqual([image["url"] for image in result["images"]], expected[:limit])
                    self.assertEqual(result["product"]["name"], "Cor Dam Abstract Sculpture")
                    self.assertEqual(result["product"]["price"], "3500")
                    self.assertEqual(result["product"]["currency"], "EUR")

    def test_nested_main_entity_excludes_recommendations_in_either_order(self):
        current = {
            "@type": "WebPage",
            "mainEntity": {
                "@type": "Product", "@id": PRODUCT_URL,
                "name": "Buffet", "image": "/current.webp",
                "offers": {"price": "0", "priceCurrency": "BRL", "url": PRODUCT_URL},
            },
        }
        related = {
            "@type": "Product", "name": "Related chair", "image": "/related.webp",
            "mainEntityOfPage": {"@id": "/produtos/chair/"},
            "offers": {"url": "/produtos/chair/", "price": "4000"},
        }
        for products in ([related, current], [current, related]):
            with self.subTest(products=products):
                soup = BeautifulSoup(product_html(products), "lxml")
                info, images = extract_jsonld_products(soup, PRODUCT_URL + "?fbclid=tracking")
                self.assertEqual(info["name"], "Buffet")
                self.assertEqual(info["price"], "0")
                self.assertEqual(images, ["https://recondicionar.com.br/current.webp"])

    def test_graph_product_fragment_and_canonical_alias_match(self):
        html = '<link rel="canonical" href="/produtos/buffet/">'
        html += product_html([{"@graph": [
            {"@type": "Product", "url": "/produtos/chair/", "name": "Related"},
            {"@type": ["Thing", "Product"], "@id": PRODUCT_URL + "#product", "name": "Buffet"},
        ]}])
        info, _ = extract_jsonld_products(BeautifulSoup(html, "lxml"), PRODUCT_URL + "alias")
        self.assertEqual(info["name"], "Buffet")

    def test_product_without_url_still_works(self):
        html = product_html([
            {"@type": "Product", "name": "Buffet", "image": "/current.webp"},
            {"@type": "Product", "url": "/produtos/chair/", "name": "Related", "image": "/related.webp"},
        ])
        info, images = extract_jsonld_products(BeautifulSoup(html, "lxml"), PRODUCT_URL)
        self.assertEqual(info["name"], "Buffet")
        self.assertEqual(images, ["https://recondicionar.com.br/current.webp"])

    def test_unrelated_product_blocks_do_not_override_page(self):
        html = product_html([{
            "@type": "Product", "url": "/produtos/chair/", "name": "Related", "image": "/related.webp",
        }])
        self.assertEqual(extract_jsonld_products(BeautifulSoup(html, "lxml"), PRODUCT_URL), ({}, []))

    def test_gallery_uses_full_links_and_lazy_srcsets_in_page_order(self):
        html = """
        <title>Buffet</title><h1>Buffet</h1>
        <img src="/logo.png">
        <div class="js-product-slide">
          <a href="/first-1024-1024.webp" data-fancybox="product-gallery">
            <img class="js-product-slide-img" src="/first-240-0.webp">
          </a>
        </div>
        <div class="js-product-slide">
          <img class="js-product-slide-img" src="data:image/gif;base64,AAAA"
            data-srcset="/second-480-0.webp 480w, /second-1024-1024.webp 1024w">
        </div>
        <div class="js-product-slide">
          <img class="js-product-slide-img" src="data:image/gif;base64,AAAA"
            srcset="/third-480-0.webp 480w, /third-1024-1024.webp 1024w">
        </div>
        <div class="js-product-slide">
          <a href="/first-1024-1024.webp" data-fancybox="product-gallery"></a>
        </div>
        <div class="js-product-slide"><img class="js-product-slide-img" src="data:image/gif;base64,AAAA"></div>
        <div class="recommendations"><img src="/related.webp"></div>
        """
        html += product_html([{"@type": "Product", "url": "/produtos/chair/", "image": "/related.webp"}])
        expected = [f"https://recondicionar.com.br/{name}-1024-1024.webp" for name in ("first", "second", "third")]
        soup = BeautifulSoup(html, "lxml")
        self.assertEqual(extract_domain_product_image_urls(soup, PRODUCT_URL), expected)
        with patch("app.services.extractor.fetch_static", return_value=(html, PRODUCT_URL)):
            result = extract(PRODUCT_URL, False, 12)
        self.assertEqual([image["url"] for image in result["images"]], expected)
        self.assertEqual(result["product"]["name"], "Buffet")


class ImageDownloadFallbackTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"IMAGE_DOWNLOAD_CDN_FALLBACK": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)
        buffer = BytesIO()
        Image.new("RGB", (20, 10), "white").save(buffer, "JPEG")
        self.content = buffer.getvalue()

    def response(self, status=200, content_type="image/jpeg"):
        response = requests.Response()
        response.status_code = status
        response.headers["content-type"] = content_type
        response._content = self.content
        response._content_consumed = True
        return response

    def test_fallback_is_restricted_to_public_origin_uploads(self):
        self.assertEqual(public_image_cdn_url(IMAGE_URL), IMAGE_URL.replace("https://", "https://i0.wp.com/", 1))
        for url in (
            IMAGE_URL + "?signature=private",
            IMAGE_URL.replace("www.urbanmodern.nl", "www.urbanmodern.nl.evil.test"),
            IMAGE_URL.replace("www.urbanmodern.nl", "private:secret@www.urbanmodern.nl"),
            IMAGE_URL.replace("/wp-content/uploads/", "/private/"),
            IMAGE_URL.replace(".jpg", ".html"),
            "https://example.com/photo.jpg",
        ):
            with self.subTest(url=url):
                self.assertEqual(public_image_cdn_url(url), "")
        with patch.dict(os.environ, {"IMAGE_DOWNLOAD_CDN_FALLBACK": "false"}):
            self.assertEqual(public_image_cdn_url(IMAGE_URL), "")

    def test_successful_direct_download_does_not_use_cdn(self):
        client = Mock()
        client.get.return_value = self.response()
        self.assertIs(image_response(IMAGE_URL, client), client.get.return_value)
        client.get.assert_called_once()

    def test_connection_failure_downloads_actual_image_bytes(self):
        client = Mock()
        client.get.side_effect = [requests.ConnectTimeout("offline"), self.response()]
        with tempfile.TemporaryDirectory() as folder:
            path, size = download_image(IMAGE_URL, Path(folder), client)
            with Image.open(path) as image:
                self.assertEqual(image.size, (20, 10))
                image.verify()
            self.assertEqual(size, len(self.content))
        self.assertEqual(client.get.call_args.args[0], public_image_cdn_url(IMAGE_URL))

    def test_gateway_error_uses_fallback_and_closes_original(self):
        client = Mock()
        original = Mock(status_code=503)
        client.get.side_effect = [original, self.response()]
        self.assertEqual(image_response(IMAGE_URL, client).status_code, 200)
        original.close.assert_called_once()

    def test_non_image_and_failed_cdn_are_rejected(self):
        for status, content_type, error in (
            (200, "text/html", ImageHostingError),
            (404, "image/jpeg", requests.HTTPError),
        ):
            with self.subTest(status=status):
                client = Mock()
                client.get.side_effect = [requests.ConnectionError("offline"), self.response(status, content_type)]
                with self.assertRaises(error):
                    image_response(IMAGE_URL, client)

    def test_other_hosts_and_disabled_fallback_preserve_network_errors(self):
        for url, enabled in ((IMAGE_URL, "false"), ("https://example.com/a.jpg", "true")):
            with self.subTest(url=url), patch.dict(os.environ, {"IMAGE_DOWNLOAD_CDN_FALLBACK": enabled}):
                client = Mock()
                client.get.side_effect = requests.ConnectionError("offline")
                with self.assertRaises(requests.ConnectionError):
                    image_response(url, client)
                client.get.assert_called_once()

    def test_missing_or_forbidden_source_is_not_retried_through_cdn(self):
        for status in (401, 403, 404, 429):
            with self.subTest(status=status):
                client = Mock()
                client.get.return_value = self.response(status)
                self.assertEqual(image_response(IMAGE_URL, client).status_code, status)
                client.get.assert_called_once()

    def test_xhs_download_uses_same_fallback(self):
        client = Mock()
        client.get.side_effect = [requests.ConnectionError("offline"), self.response()]
        with patch("app.services.xhs_pipeline.session") as factory:
            factory.return_value.__enter__.return_value = client
            image = download_xhs_image(IMAGE_URL)
        self.assertEqual(image.size, (20, 10))
        self.assertEqual(client.get.call_args.args[0], public_image_cdn_url(IMAGE_URL))


if __name__ == "__main__":
    unittest.main()
