from decimal import Decimal

from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Checkout, Product


class ProductModelTests(TestCase):
    def test_product_can_represent_500_units(self):
        product = Product.objects.create(
            name='Test smartphone', price=Decimal('499.99'), total_stock=500, available_stock=500
        )
        self.assertEqual(product.total_stock, 500)
        self.assertEqual(product.available_stock, 500)

    def test_negative_stock_fails_model_validation(self):
        product = Product(
            name='Invalid smartphone', price=Decimal('499.99'), total_stock=500, available_stock=-1
        )
        with self.assertRaises(ValidationError):
            product.full_clean()

    def test_database_rejects_available_stock_above_total(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.create(
                name='Invalid smartphone', price=Decimal('499.99'), total_stock=10, available_stock=11
            )


class CheckoutModelTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            name='Test smartphone', price=Decimal('499.99'), total_stock=500, available_stock=500
        )

    def test_idempotency_key_is_unique(self):
        Checkout.objects.create(idempotency_key='request-1', product=self.product)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Checkout.objects.create(idempotency_key='request-1', product=self.product)

    def test_checkout_statuses_are_available(self):
        self.assertEqual(
            {status for status, _label in Checkout.Status.choices},
            {'QUEUED', 'PROCESSING', 'CONFIRMED', 'REJECTED', 'FAILED'},
        )
        checkout = Checkout.objects.create(idempotency_key='request-2', product=self.product)
        self.assertEqual(checkout.status, Checkout.Status.QUEUED)


class InitializeSaleCommandTests(TestCase):
    def test_command_creates_exactly_500_units(self):
        call_command('initialize_sale', verbosity=0)
        product = Product.objects.get(name='Flash Sale Smartphone')
        self.assertEqual(product.total_stock, 500)
        self.assertEqual(product.available_stock, 500)

    def test_command_does_not_reset_existing_stock(self):
        call_command('initialize_sale', verbosity=0)
        product = Product.objects.get(name='Flash Sale Smartphone')
        product.available_stock = 321
        product.save(update_fields=['available_stock', 'updated_at'])

        call_command('initialize_sale', verbosity=0)

        product.refresh_from_db()
        self.assertEqual(product.total_stock, 500)
        self.assertEqual(product.available_stock, 321)


class ProductAPITests(TestCase):
    def test_product_list_exposes_read_only_inventory(self):
        Product.objects.create(
            name='Test smartphone', price=Decimal('499.99'), total_stock=500, available_stock=500
        )
        client = APIClient()
        response = client.get('/api/products/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]['total_stock'], 500)
        self.assertEqual(response.data[0]['available_stock'], 500)
