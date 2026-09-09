import unittest
from unittest.mock import Mock, patch

from app.services.xianyu_pipeline import (
    contains_inch_unit,
    ensure_xianyu_body_contains_country,
    extract_product_country,
    generate_xianyu_copy,
    generated_copy_uses_source_site_as_creator,
    xianyu_product_text,
    XianyuCopyValidationError,
    XianyuPipelineError,
    is_permanent_ark_error,
    normalize_dimensions_to_cm,
    normalize_inch_units_to_cm,
    normalize_xianyu_content_dimensions,
    validate_xianyu_copy,
)


class XianyuDimensionNormalizationTest(unittest.TestCase):
    def test_source_site_name_is_rejected_as_creator(self) -> None:
        product = {
            "source_url": "https://www.intro-verso.com/all-new-products/abstract-black-terracotta-sculpture-1960s",
        }

        self.assertTrue(
            generated_copy_uses_source_site_as_creator(
                "【中古预定】Abstract Black Terracotta Sculpture",
                "Intro-Verso生产的1960年代抽象陶土雕塑，值得收藏",
                product,
            )
        )
        self.assertFalse(
            generated_copy_uses_source_site_as_creator(
                "【中古预定】Abstract Black Terracotta Sculpture",
                "1960年代抽象陶土雕塑，值得收藏",
                product,
            )
        )
        self.assertNotIn(
            "intro-verso.com",
            xianyu_product_text(product, ""),
        )

    def test_product_type_must_come_from_ai_without_keyword_fallback(self) -> None:
        response = Mock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"title":"【中古预定】Abstract Black Terracotta Sculpture 1960s 抽象雕塑",'
                            '"content":"尺寸：W: 30 cm; D: 20 cm; H: 30 cm\\n'
                            '1960年代抽象黑色陶土雕塑以流动交叠的体块构成，表面深黑色包浆突出轮廓，矩形石材底座强化了作品稳定而凝练的构图，单字母标记保留了未知创作者的研究线索。作品兼具战后抽象艺术的雕塑性与建筑感，材质肌理和紧凑比例形成鲜明视觉张力，适合画廊陈设与收藏，值得收藏\\n'
                            '可预定，货期90-150天，国内运费到付，喜欢私聊",'
                            '"product_type":"","price":"30600"}'
                        )
                    }
                }
            ]
        }
        product = {
            "name": "Abstract Black Terracotta Sculpture, 1960s",
            "price": "1600",
            "currency": "EUR",
            "dimensions": "W: 30 cm; D: 20 cm; H: 30 cm",
            "product_details": {
                "description": "Abstract terracotta sculpture dating from the 1960s.",
            },
            "image_links": ["https://example.com/sculpture.jpg"],
        }

        with patch.dict("os.environ", {"ARK_API_KEY": "test-key", "XIANYU_COPY_MAX_ATTEMPTS": "1"}):
            with patch("app.services.xianyu_pipeline.requests.post", return_value=response):
                with self.assertRaisesRegex(XianyuPipelineError, "产品类型为空"):
                    generate_xianyu_copy(product)

    def test_ai_product_type_is_used_as_returned(self) -> None:
        response = Mock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"title":"【中古预定】Abstract Black Terracotta Sculpture 1960s 抽象雕塑",'
                            '"content":"尺寸：W: 30 cm; D: 20 cm; H: 30 cm\\n'
                            '1960年代抽象黑色陶土雕塑以流动交叠的体块构成，表面深黑色包浆突出轮廓，矩形石材底座强化了作品稳定而凝练的构图，单字母标记保留了未知创作者的研究线索。作品兼具战后抽象艺术的雕塑性与建筑感，材质肌理和紧凑比例形成鲜明视觉张力，适合画廊陈设与收藏，值得收藏\\n'
                            '可预定，货期90-150天，国内运费到付，喜欢私聊",'
                            '"product_type":"雕塑","price":"30600"}'
                        )
                    }
                }
            ]
        }
        product = {
            "name": "Abstract Black Terracotta Sculpture, 1960s",
            "price": "1600",
            "currency": "EUR",
            "dimensions": "W: 30 cm; D: 20 cm; H: 30 cm",
            "product_details": {
                "description": "Abstract terracotta sculpture dating from the 1960s.",
            },
            "image_links": ["https://example.com/sculpture.jpg"],
        }

        with patch.dict("os.environ", {"ARK_API_KEY": "test-key", "XIANYU_COPY_MAX_ATTEMPTS": "1"}):
            with patch("app.services.xianyu_pipeline.requests.post", return_value=response):
                _, _, product_type, _ = generate_xianyu_copy(product)

        self.assertEqual(product_type, "雕塑")

    def test_normalizes_source_dimensions_with_shared_inches_unit(self) -> None:
        self.assertEqual(normalize_dimensions_to_cm("10 x 20 x 30 inches"), "25.4 x 50.8 x 76.2 cm")

    def test_normalizes_chinese_inches_units(self) -> None:
        self.assertEqual(normalize_dimensions_to_cm("10英寸 x 20英寸 x 30英寸"), "25.4 cm x 50.8 cm x 76.2 cm")

    def test_normalizes_residual_inches_in_generated_body(self) -> None:
        product = {"dimensions": "10 x 20 x 30 inches"}
        content = normalize_xianyu_content_dimensions(
            "尺寸：10 x 20 x 30 inches\n"
            "瑞典1940年代灯具高10英寸，宽20 inch，深30″，比例清晰，值得收藏\n"
            "可预定，货期90-150天，国内运费到付，喜欢私聊",
            product,
        )

        self.assertNotIn("英寸", content)
        self.assertNotIn("inch", content.lower())
        self.assertNotIn("″", content)
        self.assertFalse(contains_inch_unit(content))
        self.assertIn("高25.4 cm，宽50.8 cm，深76.2 cm", content)

    def test_regular_quotes_are_not_treated_as_inches(self) -> None:
        self.assertFalse(contains_inch_unit('作品名为"ABC"，值得收藏'))
        self.assertFalse(contains_inch_unit("作品名为“1940年代”设计，值得收藏"))
        self.assertEqual(
            normalize_inch_units_to_cm("作品名为“1940年代”设计，值得收藏"),
            "作品名为“1940年代”设计，值得收藏",
        )

    def test_image_download_errors_are_not_retried(self) -> None:
        self.assertTrue(
            is_permanent_ark_error(
                RuntimeError("Ark HTTP 400: Timeout while downloading url=https://example.com/image.webp")
            )
        )

    def test_missing_source_country_is_normal(self) -> None:
        product = {
            "name": "Modernist Stainless Steel Bench",
            "product_details": {"description": "Modernist stainless steel bench from the 1980s."},
        }
        content = (
            "尺寸：详询\n"
            "1980年代现代主义不锈钢长凳采用开放式条板结构，线条克制利落，材质呈现冷静光泽与工业美感，整体保存状态良好，适合收藏陈设，细节处理简洁而有秩序。"
            "从照片可见其结构比例清晰，简洁造型体现包豪斯影响与时代审美，具有稳定的视觉识别度和长期收藏价值，值得收藏\n"
            "可预定，货期90-150天，国内运费到付，喜欢私聊"
        )

        self.assertEqual(extract_product_country(product), "")
        validate_xianyu_copy("【中古预定】Modernist Stainless Steel Bench", content, product, "长凳")

    def test_model_country_is_filled_when_source_country_exists(self) -> None:
        product = {"product_details": {"countryOfOrigin": "France"}}
        body = "1960年代木质沙发，结构简洁，保存状态良好，值得收藏"

        self.assertEqual(extract_product_country(product), "法国")
        self.assertTrue(ensure_xianyu_body_contains_country(body, product).startswith("法国"))

    def test_country_injection_keeps_body_within_max_length(self) -> None:
        product = {"product_details": {"Place of Origin": "France"}}
        body = "木质沙发" + ("，结构简洁、比例协调、细节克制" * 20) + "，值得收藏"

        normalized = ensure_xianyu_body_contains_country(body, product)

        self.assertLessEqual(len(normalized), 200)
        self.assertTrue(normalized.startswith("法国"))
        self.assertTrue(normalized.endswith("值得收藏"))

    def test_period_is_not_treated_as_country(self) -> None:
        product = {"product_details": {"Origin / period": "1980s"}}

        self.assertEqual(extract_product_country(product), "")


if __name__ == "__main__":
    unittest.main()
