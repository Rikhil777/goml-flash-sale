import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from flashsale.models import Product
from flashsale.services.inventory import InventoryError, initialize_inventory


class Command(BaseCommand):
    help = 'Temporarily reset the existing smartphone sale inventory to 500 in PostgreSQL and Redis.'

    def handle(self, *args, **options):
        if os.environ.get('RESET_SALE_STOCK') != 'true':
            raise CommandError('Set RESET_SALE_STOCK=true to enable this temporary reset.')

        try:
            with transaction.atomic():
                product = Product.objects.select_for_update().get(
                    name='Flash Sale Smartphone'
                )
                if product.total_stock < 500:
                    raise CommandError(
                        'Flash Sale Smartphone total_stock is below 500; inventory was not reset.'
                    )

                product.available_stock = 500
                product.save(update_fields=['available_stock', 'updated_at'])

                try:
                    redis_updated = initialize_inventory(product.pk, force=True)
                except InventoryError as exc:
                    raise CommandError(
                        f'Redis inventory reset failed; PostgreSQL update was rolled back: {exc}'
                    ) from exc

                if not redis_updated:
                    raise CommandError(
                        'Redis inventory reset did not complete; PostgreSQL update was rolled back.'
                    )
        except Product.DoesNotExist as exc:
            raise CommandError(
                'Flash Sale Smartphone does not exist; no inventory was reset.'
            ) from exc
        except Product.MultipleObjectsReturned as exc:
            raise CommandError(
                'Multiple Flash Sale Smartphone products exist; no inventory was reset.'
            ) from exc

        self.stdout.write(self.style.WARNING(
            'TEMPORARY INVENTORY RESET: '
            f'Flash Sale Smartphone (product {product.pk}) now has 500 units in PostgreSQL '
            'and Redis.'
        ))
