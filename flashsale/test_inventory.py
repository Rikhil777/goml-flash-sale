import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest import SkipTest
from unittest.mock import Mock, patch

import redis
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from flashsale.models import Product
from flashsale.services import inventory
from flashsale.services.inventory import (
    InventoryNotInitialized,
    InventoryUnavailable,
    initialize_inventory,
    inventory_key,
    reserve_inventory,
)


class RedisInventoryTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.redis_test_url = os.environ.get('REDIS_TEST_URL', 'redis://localhost:6379/15')
        cls.redis_settings = override_settings(REDIS_URL=cls.redis_test_url)
        cls.redis_settings.enable()
        try:
            inventory.get_redis_client().ping()
        except redis.RedisError as exc:
            cls.redis_settings.disable()
            raise SkipTest(f'Real Redis unavailable at REDIS_TEST_URL: {exc}') from exc

    @classmethod
    def tearDownClass(cls):
        cls.redis_settings.disable()
        super().tearDownClass()

    def setUp(self):
        self.product = Product.objects.create(
            name='Redis test smartphone',
            price=Decimal('499.99'),
            total_stock=500,
            available_stock=500,
        )
        self.key = inventory_key(self.product.pk)
        inventory.get_redis_client().delete(self.key)
        self.assertTrue(initialize_inventory(self.product.pk))

    def tearDown(self):
        inventory.get_redis_client().delete(self.key)
        super().tearDown()

    def test_inventory_initializes_from_postgresql_stock(self):
        self.assertEqual(inventory.get_redis_client().get(self.key), '500')

    def test_one_reservation_decrements_inventory(self):
        result = reserve_inventory(self.product.pk)

        self.assertTrue(result.accepted)
        self.assertEqual(result.remaining_stock, 499)
        self.assertEqual(inventory.get_redis_client().get(self.key), '499')

    def test_reservation_is_rejected_when_inventory_is_zero(self):
        inventory.get_redis_client().set(self.key, 0)

        result = reserve_inventory(self.product.pk)

        self.assertFalse(result.accepted)
        self.assertIsNone(result.reservation_id)
        self.assertEqual(result.remaining_stock, 0)
        self.assertEqual(inventory.get_redis_client().get(self.key), '0')

    def test_500_reservations_succeed_and_501st_is_rejected(self):
        results = [reserve_inventory(self.product.pk) for _ in range(501)]

        accepted = [result for result in results if result.accepted]
        self.assertEqual(len(accepted), 500)
        self.assertFalse(results[-1].accepted)
        self.assertEqual(inventory.get_redis_client().get(self.key), '0')
        self.assertGreaterEqual(min(result.remaining_stock for result in accepted), 0)

    def test_concurrent_reservations_never_exceed_500_or_go_negative(self):
        with ThreadPoolExecutor(max_workers=64) as executor:
            results = list(executor.map(lambda _index: reserve_inventory(self.product.pk), range(650)))

        successful = [result for result in results if result.accepted]
        remaining_values = [result.remaining_stock for result in successful]
        final_stock = int(inventory.get_redis_client().get(self.key))

        self.assertLessEqual(len(successful), 500)
        self.assertEqual(len(successful), 500)
        self.assertGreaterEqual(final_stock, 0)
        self.assertGreaterEqual(min(remaining_values), 0)
        self.assertEqual(final_stock, 0)

    def test_successful_reservation_identifiers_are_unique(self):
        results = [reserve_inventory(self.product.pk) for _ in range(100)]
        reservation_ids = [result.reservation_id for result in results]

        self.assertTrue(all(reservation_ids))
        self.assertEqual(len(reservation_ids), len(set(reservation_ids)))

    def test_release_restores_inventory_only_once(self):
        reservation = reserve_inventory(self.product.pk)

        self.assertTrue(inventory.release_reservation(self.product.pk, reservation.reservation_id))
        self.assertFalse(inventory.release_reservation(self.product.pk, reservation.reservation_id))
        self.assertEqual(inventory.get_redis_client().get(self.key), '500')

    def test_reservation_requires_explicit_redis_initialization(self):
        inventory.get_redis_client().delete(self.key)

        with self.assertRaises(InventoryNotInitialized):
            reserve_inventory(self.product.pk)


class RedisFailureTests(SimpleTestCase):
    def test_redis_connection_failure_becomes_controlled_inventory_error(self):
        client = Mock()
        client.eval.side_effect = redis.ConnectionError('connection refused')
        with patch.object(inventory, 'get_redis_client', return_value=client):
            with self.assertRaises(InventoryUnavailable):
                reserve_inventory(1)
