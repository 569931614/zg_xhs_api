import unittest

from app.services.cloudflare import is_cloudflare_blocked


class CloudflareDetectionTest(unittest.TestCase):
    def test_ignores_regular_script_translation_strings(self) -> None:
        html = '<script>window.labels = {"please_wait": "Please wait..."}</script>'

        self.assertFalse(
            is_cloudflare_blocked(
                'RY34 "President" highboard by Hans J. Wegner',
                "Normal product page content",
                html,
            )
        )

    def test_detects_cloudflare_challenge_title(self) -> None:
        self.assertTrue(is_cloudflare_blocked("Just a moment...", "", ""))


if __name__ == "__main__":
    unittest.main()
