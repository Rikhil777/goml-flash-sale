from django.core.management.base import BaseCommand

from flashsale.models import Product


class Command(BaseCommand):
    help = 'Create the initial smartphone sale product if it does not exist.'

    def handle(self, *args, **options):
        product, created = Product.objects.get_or_create(
            name='Flash Sale Smartphone',
            defaults={
                'description': 'Smartphone available during the flash sale.',
                'price': '499.99',
                'total_stock': 500,
                'available_stock': 500,
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS('Created Flash Sale Smartphone with 500 units.'))
        else:
            self.stdout.write('Flash Sale Smartphone already exists; existing stock was preserved.')
