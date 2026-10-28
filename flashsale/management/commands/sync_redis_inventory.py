from django.core.management.base import BaseCommand, CommandError

from flashsale.models import Product
from flashsale.services.inventory import InventoryError, initialize_inventory


class Command(BaseCommand):
    help = 'Explicitly initialize Redis inventory from a product’s PostgreSQL available stock.'

    def add_arguments(self, parser):
        parser.add_argument('--product-id', type=int, required=True)
        parser.add_argument(
            '--force',
            action='store_true',
            help='Replace an existing Redis inventory value with PostgreSQL available_stock.',
        )

    def handle(self, *args, **options):
        product_id = options['product_id']
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist as exc:
            raise CommandError(f'Product {product_id} does not exist.') from exc

        try:
            initialized = initialize_inventory(product.pk, force=options['force'])
        except InventoryError as exc:
            raise CommandError(str(exc)) from exc

        if initialized:
            self.stdout.write(self.style.SUCCESS(
                f'Redis inventory for product {product.pk} is {product.available_stock}.'
            ))
        else:
            self.stdout.write(
                f'Redis inventory for product {product.pk} already exists; it was preserved. '
                'Use --force to replace it from PostgreSQL.'
            )
