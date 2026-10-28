from django.test import SimpleTestCase
from django.urls import reverse


class FlashSalePageTests(SimpleTestCase):
    def test_root_renders_the_lightweight_sale_page_and_local_assets(self):
        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="checkout-button"')
        self.assertContains(response, 'aria-live="polite"')
        self.assertContains(response, 'flashsale/css/style.css')
        self.assertContains(response, 'flashsale/js/app.js')
