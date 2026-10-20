from django.test import TestCase
from django.urls import resolve


class CaptchaApiTests(TestCase):
    def test_get_captcha_returns_resolvable_image_url(self):
        response = self.client.get("/api/user/get_captcha")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        data = payload["data"]
        self.assertTrue(data["key"])
        self.assertTrue(data["image_url"].startswith("/captcha/image/"))
        self.assertEqual(resolve(data["image_url"]).url_name, "captcha-image")
