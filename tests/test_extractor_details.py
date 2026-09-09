import unittest
from unittest.mock import Mock

from bs4 import BeautifulSoup

from app.services.extractor import (
    extract_jsonld_products,
    extract_domain_product_info,
    extract_dom_product_info,
    extract_dimensions_from_lines,
    extract_dimensions_from_text,
    format_dimension_cm,
    jina_reader_url,
    normalize_detail_label,
    normalize_dimension_text_to_cm,
    normalize_sitonvintage_dimensions,
    normalize_studio_1_dimensions,
    try_monument_items_api,
)


class ExtractorDetailLabelTest(unittest.TestCase):
    def test_monument_items_api_extracts_product_payload(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "success": True,
            "item": {
                "displayName": "Mirror by Sōri Yanagi for Akita Mokko",
                "description": "Minimalist stained beech mirror.",
                "price": "3600",
                "images": [
                    "https://cdn.example.com/mirror-1.jpg",
                    "https://cdn.example.com/mirror-2.jpg",
                ],
                "dimensions": "H76 x W60 x D4",
                "condition": "Very good condition",
                "period": "1976",
                "material": "Beech, glass",
                "manufacturer": "Akita Mokko",
                "literature": "Forms Born of Sori Yanagi",
                "collection": ["Furniture"],
                "category": ["Furniture"],
                "type": "For sale",
                "availability": "Available",
            },
        }
        session = Mock()
        session.get.return_value = response

        result = try_monument_items_api(
            "https://www.monumentgallery.co.uk/product/mirror-by-sori-yanagi-for-akita-mokko-d25c51c168",
            session,
            12,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["product"]["name"], "Mirror by Sōri Yanagi for Akita Mokko")
        self.assertEqual(result["product"]["price"], "3600")
        self.assertEqual(result["product"]["currency"], "GBP")
        self.assertEqual(result["product"]["dimensions"], "W: 60 cm; D: 4 cm; H: 76 cm")
        self.assertEqual(result["product"]["details"]["Manufacturer"], "Akita Mokko")
        self.assertEqual(result["product"]["details"]["Collection"], ["Furniture"])
        self.assertEqual(len(result["images"]), 2)
        session.get.assert_called_once_with(
            "https://www.monumentgallery.co.uk/api/items?slug=mirror-by-sori-yanagi-for-akita-mokko-d25c51c168",
            timeout=12,
            headers={"Accept": "application/json"},
        )

    def test_jina_reader_url_preserves_source_scheme(self) -> None:
        self.assertEqual(
            jina_reader_url("https://example.com/products/item"),
            "https://r.jina.ai/https://example.com/products/item",
        )

    def test_source_site_is_not_used_as_creator_or_brand(self) -> None:
        html = """
        <html>
          <head>
            <title>Abstract Black Terracotta Sculpture | Intro Verso</title>
            <script type="application/ld+json">
              {
                "@type": "Product",
                "name": "Abstract Black Terracotta Sculpture | Intro Verso",
                "brand": {"@type": "Brand", "name": "Intro Verso"},
                "additionalProperty": [
                  {"name": "Manufacturer", "value": "Intro Verso"},
                  {"name": "Designer", "value": "Actual Artist"}
                ]
              }
            </script>
          </head>
          <body>
            <h1>Abstract Black Terracotta Sculpture | Intro Verso</h1>
            <p>Manufacturer: Intro Verso</p>
            <p>Designer: Actual Artist</p>
          </body>
        </html>
        """
        soup = BeautifulSoup(html, "lxml")
        source_url = "https://www.intro-verso.com/all-new-products/abstract-black-terracotta-sculpture-1960s"

        dom_result = extract_dom_product_info(soup, source_url)
        jsonld_result, _ = extract_jsonld_products(soup, source_url)

        self.assertEqual(dom_result["name"], "Abstract Black Terracotta Sculpture")
        self.assertNotIn("Manufacturer", dom_result.get("details", {}))
        self.assertEqual(dom_result["details"]["Designer"], "Actual Artist")
        self.assertEqual(jsonld_result["name"], "Abstract Black Terracotta Sculpture")
        self.assertNotIn("brand", jsonld_result)
        self.assertNotIn("Brand", jsonld_result.get("details", {}))
        self.assertNotIn("Manufacturer", jsonld_result.get("details", {}))
        self.assertEqual(jsonld_result["details"]["Designer"], "Actual Artist")

    def test_normalizes_origin_field_variants(self) -> None:
        for label in (
            "countryOfOrigin",
            "country_of_origin",
            "country-of-origin",
            "placeOfOrigin",
            "madeIn",
        ):
            with self.subTest(label=label):
                self.assertEqual(normalize_detail_label(label), "Place of Origin")

    def test_keeps_generic_country_labels_usable(self) -> None:
        self.assertEqual(normalize_detail_label("Country"), "Country")
        self.assertEqual(normalize_detail_label("Origin"), "Origin")

    def test_measurements_with_qualifier_are_dimensions(self) -> None:
        self.assertEqual(normalize_detail_label("Measurements including frame"), "dimensions")
        self.assertEqual(
            extract_dimensions_from_lines(["Measurements including frame:", "H:143 W:143 D:3,5"]),
            "W: 143 cm; D: 3.5 cm; H: 143 cm",
        )

    def test_studio_1_converts_meter_dimensions_to_centimeters(self) -> None:
        self.assertEqual(
            normalize_studio_1_dimensions("w: 0,305 ; d: 0,135 ; h: 0,505"),
            "W: 30.5 cm; D: 13.5 cm; H: 50.5 cm",
        )

    def test_studio_1_extracts_price_and_details(self) -> None:
        html = """
        <html><body>
          <h1>Kazuhide Takahama Nefer 1 Table Lamp for Sirrah Italy 1980s</h1>
          <h3>Price € 1500,-</h3>
          <ul>
            <li class="el-item"><div class="el-title">Manufacturer:</div><div class="el-content">Sirrah</div></li>
            <li class="el-item"><div class="el-title">Country:</div><div class="el-content">Italy</div></li>
            <li class="el-item"><div class="el-title">Dimensions:</div><div class="el-content">w: 0,305 ; d: 0,135 ; h: 0,505</div></li>
          </ul>
        </body></html>
        """

        result = extract_domain_product_info(
            BeautifulSoup(html, "lxml"),
            "https://studio-1.nl/lighting/table-lamp/example",
        )

        self.assertEqual(result["price"], "€ 1500")
        self.assertEqual(result["currency"], "EUR")
        self.assertEqual(result["dimensions"], "W: 30.5 cm; D: 13.5 cm; H: 50.5 cm")
        self.assertEqual(result["details"]["Country"], "Italy")

    def test_sitonvintage_extracts_seo_price_and_dimensions(self) -> None:
        html = """
        <html><head>
          <meta name="description" content="Vintage coffee table. Dimensions: 40cm H x 140cm W x 140cm D. Price: On request" />
        </head><body>
          <h1 class="product_title">Artona Africa Coffee Table by Afra &amp; Tobia Scarpa for Maxalto, 1970s</h1>
        </body></html>
        """

        result = extract_domain_product_info(
            BeautifulSoup(html, "lxml"),
            "https://www.sitonvintage.com/product/example/",
        )

        self.assertEqual(
            normalize_sitonvintage_dimensions("40cm H x 140cm W x 140cm D"),
            "W: 140 cm; D: 140 cm; H: 40 cm",
        )
        self.assertEqual(result["dimensions"], "W: 140 cm; D: 140 cm; H: 40 cm")
        self.assertEqual(result["price"], "On request")

    def test_incorsostudio_extracts_inches_dimensions_as_centimeters(self) -> None:
        html = """
        <html><body>
          <h1>Iron and Leather Loveseat</h1>
          <div class="product__description">
            <p>Vintage Steel and leather loveseat. Unique organic shapes throughout, with beautiful detailing throughout.</p>
            <p>DIMENSIONS</p>
            <p>28&quot; D X 58 1/2&quot; W X 35&quot; H X 18&quot; Seat Height</p>
            <p>ORIGIN: Vintage, manufacturing date and location unknown.</p>
          </div>
        </body></html>
        """

        result = extract_dom_product_info(
            BeautifulSoup(html, "lxml"),
            "https://incorsostudio.com/products/iron-and-leather-loveseat",
        )

        self.assertEqual(
            result["dimensions"],
            "W: 148.6 cm; D: 71.1 cm; H: 88.9 cm; Seat Height: 45.7 cm",
        )
        self.assertEqual(result["details"]["Origin"], "Vintage, manufacturing date and location unknown.")

    def test_altar_extracts_meta_price_and_dimensions(self) -> None:
        html = """
        <html><head>
          <meta name="description" content="A severe cabinet. Height : approx. 242 cm Length : approx. 44 cm Width : approx. 44 cm." />
          <meta property="product:price:amount" content="9250.00" />
          <meta property="product:price:currency" content="GBP" />
        </head><body>
          <h1>Tage Andersen Iron and Cobalt Stained Glass Cabinet</h1>
        </body></html>
        """

        result = extract_domain_product_info(
            BeautifulSoup(html, "lxml"),
            "https://www.altar.london/shop/p/tage-andersen-iron-and-cobalt-stained-glass-cabinet",
        )

        self.assertEqual(result["price"], "9250.00")
        self.assertEqual(result["currency"], "GBP")
        self.assertEqual(result["dimensions"], "W: 44 cm; H: 242 cm; L: 44 cm")

    def test_normalizes_common_dimension_sequences(self) -> None:
        self.assertEqual(
            normalize_dimension_text_to_cm("Dimensions (cm, approx):Height: 50 Diameter: 25Condition: Good"),
            "H: 50 cm; Diameter: 25 cm",
        )
        self.assertEqual(normalize_dimension_text_to_cm("WDH 50 × 13 × 50 cm"), "W: 50 cm; D: 13 cm; H: 50 cm")
        self.assertEqual(
            normalize_dimension_text_to_cm("180 x 49 x 130 cm (L x W x H)"),
            "W: 49 cm; H: 130 cm; L: 180 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("H. 226 x 162 x 43 cm"),
            "W: 162 cm; D: 43 cm; H: 226 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("3.9 in (ø) x 9.4 in (H)"),
            "H: 23.9 cm; Diameter: 9.9 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("55cm x 33cm"),
            "W: 55 cm; H: 33 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("35 × 35 × 1 cm"),
            "W: 35 cm; D: 35 cm; H: 1 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("H75xW46x46cm"),
            "W: 46 cm; D: 46 cm; H: 75 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("Haut. 184 x Larg. 22 Prof. 22 cm"),
            "W: 22 cm; D: 22 cm; H: 184 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("Dimensão: 71 x 89 x 80 cm Altura do assento: 35 cm"),
            "W: 71 cm; D: 89 cm; H: 80 cm; Seat Height: 35 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("Visible size 42×59 cm"),
            "W: 42 cm; H: 59 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm(
                "A mid 20th century table lamp. Dimensions (cm, approx): Height: 50 Diameter: 25 Condition: In vintage condition."
            ),
            "H: 50 cm; Diameter: 25 cm",
        )
        self.assertEqual(
            extract_dimensions_from_lines(["Größe (cm):", "Durchmesser: 28", "Andere Kunden haben gesucht:"]),
            "Diameter: 28 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("Length: 98/196 cm. Width: 39 cm. Height: 79 cm."),
            "W: 39 cm; H: 79 cm; L: 98/196 cm",
        )
        self.assertEqual(
            extract_dimensions_from_lines(["W", "130 cm ·", "D", "130 cm ·", "H", "25/35 cm", "Pricing"]),
            "W: 130 cm; D: 130 cm; H: 25/35 cm",
        )
        self.assertEqual(format_dimension_cm("1/2", "in"), "1.3")
        self.assertEqual(format_dimension_cm("9.84/13.78", "in"), "25/35")
        self.assertEqual(
            extract_dimensions_from_lines(
                [
                    "Dimensions: 6",
                    "5cm high x 90cm deep x 80cm wide x seat height 40cm x seat depth 60cm",
                ]
            ),
            "W: 80 cm; D: 90 cm; H: 65 cm; Seat Height: 40 cm; Seat Depth: 60 cm",
        )
        self.assertEqual(
            extract_dimensions_from_lines(
                ["Measurements:", "– Height: 74 cm", "– Width: 70 cm", "– Depth: 55 cm", "– S. Height: 45 cm"]
            ),
            "W: 70 cm; D: 55 cm; H: 74 cm; Seat Height: 45 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("Dimensions 17,5 Width 15 Depth 56 Height"),
            "W: 17.5 cm; D: 15 cm; H: 56 cm",
        )
        self.assertEqual(
            normalize_dimension_text_to_cm("W160 x D260"),
            "W: 160 cm; D: 260 cm",
        )

    def test_does_not_treat_prose_as_dimensions(self) -> None:
        self.assertEqual(
            extract_dimensions_from_text("A mid 20th century table lamp with a perforated tole shade fixed to a domed iron base."),
            "",
        )

    def test_stops_dimension_scan_before_related_products(self) -> None:
        self.assertEqual(
            extract_dimensions_from_lines(
                [
                    "Produktion von JVC",
                    "Related products",
                    "Antiker Sarab Teppich Baumwolle Wolle Feiner Knoten Iran 316 x 91 cm",
                    "316 x 91 cm",
                ]
            ),
            "",
        )

    def test_studio_1_extracts_table_details(self) -> None:
        html = """
        <html><head><title>Stilux Milano Table Lamp 50s</title></head><body>
          <p><strong>STILUX MILANO TABLE LAMP 50s</strong></p>
          <p>Price: € 150,-</p>
          <table><tbody>
            <tr><td>Country:</td><td>&nbsp;</td><td>Italy</td></tr>
            <tr><td>Dimensions:</td><td>&nbsp;</td><td>max. h: 0,33 ; length shade: 0,12</td></tr>
          </tbody></table>
        </body></html>
        """

        result = extract_domain_product_info(
            BeautifulSoup(html, "lxml"),
            "https://studio-1.nl/lighting/table-lamp/stilux-milano-table-lamp-50s",
        )

        self.assertEqual(result["price"], "€ 150")
        self.assertEqual(result["dimensions"], "H: 33 cm; L: 12 cm")
        self.assertEqual(result["details"]["Country"], "Italy")


if __name__ == "__main__":
    unittest.main()
