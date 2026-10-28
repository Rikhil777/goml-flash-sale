from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import SkipTest
from unittest.mock import patch
from uuid import uuid4

from celery.exceptions import OperationalError as CeleryOperationalError
from django.db import connections
from django.test import TestCase, TransactionTestCase
from kombu import Connection
from rest_framework.test import APIClient

from config.celery import app as celery_app
from flashsale.models import Checkout, Product
from flashsale.services.inventory import InventoryUnavailable, ReservationResult
from flashsale.tasks import process_checkout


class CheckoutAPITests(TransactionTestCase):
    def setUp(self):
        self.product = Product.objects.create(
            name='Checkout test smartphone',
            price=Decimal('499.99'),
            total_stock=500,
            available_stock=500,
        )
        self.client = APIClient()
        self.payload = {'product_id': self.product.pk, 'idempotency_key': 'checkout-request-1'}
        self.reservation = ReservationResult(True, 'reservation-123', 499)

    @patch('flashsale.views.process_checkout.apply_async')
    @patch('flashsale.views.reserve_inventory')
    def test_successful_checkout_is_queued_and_returns_202(self, reserve, enqueue):
        events = []
        close_connections = connections.close_all

        def close_and_record():
            events.append('close')
            close_connections()

        def reserve_after_connection_closed(product_id):
            self.assertEqual(product_id, self.product.pk)
            events.append('redis')
            return self.reservation

        def enqueue_after_commit(*, args):
            events.append('rabbitmq')

        reserve.side_effect = reserve_after_connection_closed
        enqueue.side_effect = enqueue_after_commit

        with patch.object(connections, 'close_all', side_effect=close_and_record):
            response = self.client.post('/api/checkout/', self.payload, format='json')

        checkout = Checkout.objects.get(idempotency_key=self.payload['idempotency_key'])
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data, {'checkout_id': checkout.pk, 'status': 'QUEUED'})
        self.assertEqual(checkout.status, Checkout.Status.QUEUED)
        self.assertEqual(checkout.reservation_id, self.reservation.reservation_id)
        reserve.assert_called_once_with(self.product.pk)
        enqueue.assert_called_once_with(args=[checkout.pk])
        # In-memory SQLite intentionally ignores close(), so assert the ordering
        # of the production connection-close calls around external I/O instead.
        self.assertEqual(events, ['close', 'redis', 'close', 'rabbitmq'])

    @patch('flashsale.views.process_checkout.apply_async')
    @patch('flashsale.views.reserve_inventory')
    def test_concurrent_duplicate_requests_share_one_reservation(self, reserve, enqueue):
        reservation_started = Event()
        allow_reservation = Event()

        def slow_reservation(_product_id):
            reservation_started.set()
            if not allow_reservation.wait(timeout=5):
                raise TimeoutError('test reservation gate timed out')
            return self.reservation

        reserve.side_effect = slow_reservation

        def post_checkout():
            return APIClient().post('/api/checkout/', self.payload, format='json')

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_request = executor.submit(post_checkout)
            self.assertTrue(reservation_started.wait(timeout=5))
            duplicate_request = executor.submit(post_checkout)
            duplicate_response = duplicate_request.result(timeout=5)
            allow_reservation.set()
            first_response = first_request.result(timeout=5)

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertEqual(Checkout.objects.filter(idempotency_key=self.payload['idempotency_key']).count(), 1)
        reserve.assert_called_once_with(self.product.pk)
        enqueue.assert_called_once()

    @patch('flashsale.views.process_checkout.apply_async')
    @patch('flashsale.views.reserve_inventory')
    def test_duplicate_idempotency_key_does_not_reserve_or_enqueue_again(self, reserve, enqueue):
        reserve.return_value = self.reservation
        first = self.client.post('/api/checkout/', self.payload, format='json')
        second = self.client.post('/api/checkout/', self.payload, format='json')

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Checkout.objects.filter(idempotency_key=self.payload['idempotency_key']).count(), 1)
        reserve.assert_called_once_with(self.product.pk)
        enqueue.assert_called_once()

    def test_status_endpoint_returns_only_checkout_status_fields(self):
        checkout = Checkout.objects.create(
            idempotency_key='status-checkout', product=self.product, status=Checkout.Status.CONFIRMED
        )

        response = self.client.get(f'/api/checkout/{checkout.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            'checkout_id': checkout.pk,
            'product_id': self.product.pk,
            'status': 'CONFIRMED',
            'failure_reason': None,
        })

    def test_nonexistent_product_returns_404(self):
        payload = {'product_id': self.product.pk + 100, 'idempotency_key': 'missing-product'}
        response = self.client.post('/api/checkout/', payload, format='json')
        self.assertEqual(response.status_code, 404)

    def test_missing_idempotency_key_returns_400(self):
        response = self.client.post('/api/checkout/', {'product_id': self.product.pk}, format='json')
        self.assertEqual(response.status_code, 400)

    @patch('flashsale.views.reserve_inventory', side_effect=InventoryUnavailable())
    def test_redis_failure_is_a_controlled_service_unavailable_response(self, reserve):
        response = self.client.post('/api/checkout/', self.payload, format='json')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['error'], 'inventory_unavailable')
        self.assertEqual(Checkout.objects.get(idempotency_key=self.payload['idempotency_key']).status,
                         Checkout.Status.FAILED)
        reserve.assert_called_once_with(self.product.pk)

    @patch('flashsale.views.release_reservation')
    @patch('flashsale.views.process_checkout.apply_async', side_effect=CeleryOperationalError('broker down'))
    @patch('flashsale.views.reserve_inventory')
    def test_enqueue_failure_marks_failed_and_releases_reservation(self, reserve, enqueue, release):
        reserve.return_value = self.reservation

        response = self.client.post('/api/checkout/', self.payload, format='json')

        checkout = Checkout.objects.get(idempotency_key=self.payload['idempotency_key'])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['status'], Checkout.Status.FAILED)
        self.assertEqual(checkout.status, Checkout.Status.FAILED)
        self.assertEqual(checkout.reservation_id, self.reservation.reservation_id)
        release.assert_called_once_with(self.product.pk, self.reservation.reservation_id)
        reserve.assert_called_once_with(self.product.pk)
        enqueue.assert_called_once()

        retry_response = self.client.post('/api/checkout/', self.payload, format='json')
        self.assertEqual(retry_response.status_code, 200)
        reserve.assert_called_once()
        enqueue.assert_called_once()

    @patch('flashsale.views.process_checkout.apply_async')
    @patch('flashsale.views.reserve_inventory')
    def test_sold_out_redis_inventory_is_rejected(self, reserve, enqueue):
        reserve.return_value = ReservationResult(False, None, 0)

        response = self.client.post('/api/checkout/', self.payload, format='json')

        checkout = Checkout.objects.get(idempotency_key=self.payload['idempotency_key'])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(checkout.status, Checkout.Status.REJECTED)
        enqueue.assert_not_called()


class CheckoutTaskTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            name='Worker test smartphone',
            price=Decimal('499.99'),
            total_stock=500,
            available_stock=1,
        )

    def make_checkout(self, key):
        return Checkout.objects.create(
            idempotency_key=key,
            product=self.product,
            status=Checkout.Status.QUEUED,
            reservation_id=f'reservation-{key}',
        )

    def test_duplicate_task_delivery_decrements_postgresql_once(self):
        checkout = self.make_checkout('worker-idempotency')

        first = process_checkout.apply(args=[checkout.pk]).get()
        second = process_checkout.apply(args=[checkout.pk]).get()

        self.product.refresh_from_db()
        checkout.refresh_from_db()
        self.assertEqual(first, Checkout.Status.CONFIRMED)
        self.assertEqual(second, Checkout.Status.CONFIRMED)
        self.assertEqual(self.product.available_stock, 0)
        self.assertEqual(checkout.status, Checkout.Status.CONFIRMED)

    def test_conditional_database_update_rejects_without_negative_stock(self):
        first = self.make_checkout('worker-one')
        second = self.make_checkout('worker-two')

        process_checkout.apply(args=[first.pk]).get()
        process_checkout.apply(args=[second.pk]).get()

        self.product.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.product.available_stock, 0)
        self.assertEqual({first.status, second.status}, {Checkout.Status.CONFIRMED, Checkout.Status.REJECTED})
        rejected = first if first.status == Checkout.Status.REJECTED else second
        self.assertIn('No PostgreSQL inventory', rejected.failure_reason)


class RabbitMQIntegrationTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = Connection(celery_app.conf.broker_url, connect_timeout=2)
        try:
            cls.connection.ensure_connection(max_retries=0)
        except Exception as exc:
            cls.connection.release()
            raise SkipTest(f'Real RabbitMQ broker unavailable: {exc}') from exc

    @classmethod
    def tearDownClass(cls):
        cls.connection.release()
        super().tearDownClass()

    def test_task_can_be_published_to_real_rabbitmq(self):
        queue_name = f'phase3-test-{uuid4().hex}'
        try:
            process_checkout.apply_async(args=[-1], queue=queue_name)
            self.assertTrue(self.connection.channel().queue_declare(queue=queue_name, passive=True).message_count >= 1)
        finally:
            self.connection.channel().queue_delete(queue_name)
