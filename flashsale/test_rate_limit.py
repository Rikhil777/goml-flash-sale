from decimal import Decimal
from unittest.mock import patch

import redis
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from flashsale.models import Product
from flashsale.services.inventory import ReservationResult
from flashsale.services.rate_limit import checkout_rate_limit_key


@override_settings(CHECKOUT_RATE_LIMIT_REQUESTS=2, CHECKOUT_RATE_LIMIT_WINDOW=30)
class CheckoutRateLimitTests(TransactionTestCase):
    def setUp(self):
        self.product = Product.objects.create(
            name='Rate limit test smartphone',
            price=Decimal('499.99'),
            total_stock=500,
            available_stock=500,
        )

    def post_checkout(self, key, remote_addr='192.0.2.10'):
        return APIClient().post(
            '/api/checkout/',
            {'product_id': self.product.pk, 'idempotency_key': key},
            format='json',
            REMOTE_ADDR=remote_addr,
        )

    @patch('flashsale.views.process_checkout.apply_async')
    @patch('flashsale.views.reserve_inventory')
    @patch('flashsale.services.rate_limit.get_redis_client')
    def test_requests_below_limit_are_accepted(self, client_factory, reserve, enqueue):
        client_factory.return_value.eval.side_effect = [[1, 30], [2, 30]]
        reserve.side_effect = [
            ReservationResult(True, 'reservation-1', 499),
            ReservationResult(True, 'reservation-2', 498),
        ]

        responses = [self.post_checkout(f'below-limit-{index}') for index in range(2)]

        self.assertEqual([response.status_code for response in responses], [202, 202])
        self.assertEqual(client_factory.return_value.eval.call_count, 2)
        self.assertEqual(reserve.call_count, 2)
        self.assertEqual(enqueue.call_count, 2)

    @patch('flashsale.views.process_checkout.apply_async')
    @patch('flashsale.views.reserve_inventory')
    @patch('flashsale.services.rate_limit.get_redis_client')
    def test_requests_over_limit_receive_429_and_retry_after(self, client_factory, reserve, enqueue):
        client_factory.return_value.eval.side_effect = [[1, 30], [1, 27], [0, 27]]
        reserve.side_effect = [
            ReservationResult(True, 'reservation-1', 499),
            ReservationResult(True, 'reservation-2', 498),
        ]

        responses = [self.post_checkout(f'over-limit-{index}') for index in range(3)]

        self.assertEqual([response.status_code for response in responses], [202, 202, 429])
        self.assertEqual(responses[2]['Retry-After'], '27')
        self.assertEqual(responses[2].data['error'], 'rate_limited')
        self.assertEqual(reserve.call_count, 2)
        self.assertEqual(enqueue.call_count, 2)

    @patch('flashsale.views.process_checkout.apply_async')
    @patch('flashsale.views.reserve_inventory')
    @patch('flashsale.services.rate_limit.get_redis_client')
    def test_client_ip_addresses_have_independent_limits(self, client_factory, reserve, _enqueue):
        client_factory.return_value.eval.side_effect = [[1, 30], [1, 30]]
        reserve.side_effect = [
            ReservationResult(True, 'reservation-1', 499),
            ReservationResult(True, 'reservation-2', 498),
        ]

        first = self.post_checkout('ip-a', '192.0.2.11')
        second = self.post_checkout('ip-b', '192.0.2.12')

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        first_key = client_factory.return_value.eval.call_args_list[0].args[2]
        second_key = client_factory.return_value.eval.call_args_list[1].args[2]
        self.assertNotEqual(first_key, second_key)
        self.assertNotIn('192.0.2.11', first_key)

    @override_settings(CHECKOUT_RATE_LIMIT_CLIENT_IP_HEADER='HTTP_X_REAL_IP')
    @patch('flashsale.views.process_checkout.apply_async')
    @patch('flashsale.views.reserve_inventory')
    @patch('flashsale.services.rate_limit.get_redis_client')
    def test_configured_trusted_proxy_header_identifies_the_client(self, client_factory, reserve, _enqueue):
        client_factory.return_value.eval.return_value = [1, 30]
        reserve.return_value = ReservationResult(True, 'reservation-proxy', 499)

        response = APIClient().post(
            '/api/checkout/',
            {'product_id': self.product.pk, 'idempotency_key': 'trusted-proxy-ip'},
            format='json',
            REMOTE_ADDR='172.20.0.2',
            HTTP_X_REAL_IP='192.0.2.50',
        )

        self.assertEqual(response.status_code, 202)
        used_key = client_factory.return_value.eval.call_args.args[2]
        self.assertEqual(used_key, checkout_rate_limit_key('192.0.2.50'))

    @patch('flashsale.views.process_checkout.apply_async')
    @patch('flashsale.views.reserve_inventory')
    @patch('flashsale.services.rate_limit.get_redis_client')
    def test_rate_limiter_redis_failure_fails_open(self, client_factory, reserve, enqueue):
        client_factory.return_value.eval.side_effect = redis.ConnectionError('redis unavailable')
        reserve.return_value = ReservationResult(True, 'reservation-allowed', 499)

        response = self.post_checkout('redis-rate-limit-failure')

        self.assertEqual(response.status_code, 202)
        reserve.assert_called_once_with(self.product.pk)
        enqueue.assert_called_once()
