from __future__ import annotations

import unittest

from app.services.xhs_pipeline import (
    build_xhs_hashtag_line,
    extract_xhs_ai_category,
    generated_xhs_copy_uses_source_site_as_creator,
    normalize_xhs_copy_content,
)


class XHSPipelineHashtagTests(unittest.TestCase):
    def test_source_site_name_is_not_treated_as_creator(self) -> None:
        product = {
            "source_url": "https://www.intro-verso.com/all-new-products/abstract-black-terracotta-sculpture-1960s",
        }

        self.assertTrue(
            generated_xhs_copy_uses_source_site_as_creator(
                "旨丘｜Abstract Black Terracotta Sculpture",
                "Intro-Verso生产的1960年代抽象陶土雕塑，值得收藏",
                product,
            )
        )
        self.assertFalse(
            generated_xhs_copy_uses_source_site_as_creator(
                "旨丘｜Abstract Black Terracotta Sculpture",
                "1960年代抽象陶土雕塑，值得收藏",
                product,
            )
        )

    def test_build_xhs_hashtag_line_uses_ai_category(self) -> None:
        self.assertEqual(
            build_xhs_hashtag_line("餐边柜"),
            "#中古家具[话题]# #上海中古家具[话题]# #广州中古家具[话题]# #深圳中古家具[话题]# #北京中古家具[话题]# #室内设计师[话题]# #装修[话题]# #餐边柜[话题]# #中古餐边柜[话题]#",
        )

    def test_extract_xhs_ai_category(self) -> None:
        category, content = extract_xhs_ai_category("品类：雕塑\n\n正文内容")
        self.assertEqual(category, "雕塑")
        self.assertEqual(content, "正文内容")

    def test_extract_xhs_ai_category_before_title_keeps_title_and_body_order(self) -> None:
        content = "品类：餐桌\n\n旨丘｜橡木餐桌·新艺术之美\n\n正文内容"
        category, content_without_category = extract_xhs_ai_category(content)
        parts = [part.strip() for part in content_without_category.splitlines() if part.strip()]

        self.assertEqual(category, "餐桌")
        self.assertEqual(parts[0], "旨丘｜橡木餐桌·新艺术之美")
        self.assertEqual(content_without_category[len(parts[0]):].strip(), "正文内容")

    def test_build_xhs_hashtag_line_uses_ai_sculpture_category(self) -> None:
        self.assertEqual(
            build_xhs_hashtag_line("雕塑"),
            "#中古家具[话题]# #上海中古家具[话题]# #广州中古家具[话题]# #深圳中古家具[话题]# #北京中古家具[话题]# #室内设计师[话题]# #装修[话题]# #雕塑[话题]# #中古雕塑[话题]#",
        )

    def test_normalize_xhs_copy_content_replaces_trailing_hashtags(self) -> None:
        content = "正文第一段\n\n#中古家具[话题]# #上海中古家具[话题]#"

        self.assertEqual(
            normalize_xhs_copy_content(content, "雕塑"),
            "正文第一段\n\n#中古家具[话题]# #上海中古家具[话题]# #广州中古家具[话题]# #深圳中古家具[话题]# #北京中古家具[话题]# #室内设计师[话题]# #装修[话题]# #雕塑[话题]# #中古雕塑[话题]#",
        )

    def test_normalize_xhs_copy_content_removes_prefixed_and_split_topic_lines(self) -> None:
        content = (
            "正文第一段\n\n"
            "话题：#中古家具[话题]# #雕塑[话题]#\n"
            "#中古雕塑[话题]#\n\n"
            "正文第二段"
        )

        normalized = normalize_xhs_copy_content(content, "雕塑")

        self.assertEqual(
            normalized,
            "正文第一段\n\n正文第二段\n\n"
            "#中古家具[话题]# #上海中古家具[话题]# #广州中古家具[话题]# "
            "#深圳中古家具[话题]# #北京中古家具[话题]# #室内设计师[话题]# "
            "#装修[话题]# #雕塑[话题]# #中古雕塑[话题]#",
        )

    def test_normalize_xhs_copy_content_removes_topics_mixed_with_body_text(self) -> None:
        content = "正文第一段 #中古家具[话题]# 继续描述\n\n话题：#雕塑[话题]#"

        normalized = normalize_xhs_copy_content(content, "雕塑")

        self.assertEqual(
            normalized.split("\n\n")[0],
            "正文第一段 继续描述",
        )

    def test_normalize_xhs_copy_content_keeps_each_final_topic_once(self) -> None:
        content = (
            "#中古家具[话题]# #上海中古家具[话题]#\n"
            "正文\n"
            "#中古家具[话题]# #雕塑[话题]#\n"
            "话题：#中古雕塑[话题]#"
        )

        normalized = normalize_xhs_copy_content(content, "雕塑")

        for tag in (
            "#中古家具[话题]#",
            "#上海中古家具[话题]#",
            "#广州中古家具[话题]#",
            "#深圳中古家具[话题]#",
            "#北京中古家具[话题]#",
            "#室内设计师[话题]#",
            "#装修[话题]#",
            "#雕塑[话题]#",
            "#中古雕塑[话题]#",
        ):
            self.assertEqual(normalized.count(tag), 1, tag)

    def test_normalize_xhs_copy_content_removes_topic_without_closing_hash(self) -> None:
        content = "正文\n\n#中古餐桌[话题]"

        normalized = normalize_xhs_copy_content(content, "餐桌")

        self.assertNotIn("#中古餐桌[话题]", normalized.split("\n\n")[0])
        self.assertEqual(normalized.count("#中古餐桌[话题]#"), 1)


if __name__ == "__main__":
    unittest.main()
