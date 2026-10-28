from django.core.validators import MinValueValidator
from django.db import models


class Product(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    total_stock = models.PositiveIntegerField()
    available_stock = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(price__gte=0), name='product_price_nonnegative'),
            models.CheckConstraint(condition=models.Q(total_stock__gte=0), name='product_total_stock_nonnegative'),
            models.CheckConstraint(condition=models.Q(available_stock__gte=0), name='product_available_stock_nonnegative'),
            models.CheckConstraint(condition=models.Q(available_stock__lte=models.F('total_stock')), name='product_available_stock_lte_total'),
        ]

    def __str__(self):
        return self.name


class Checkout(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'QUEUED', 'Queued'
        PROCESSING = 'PROCESSING', 'Processing'
        CONFIRMED = 'CONFIRMED', 'Confirmed'
        REJECTED = 'REJECTED', 'Rejected'
        FAILED = 'FAILED', 'Failed'

    idempotency_key = models.CharField(max_length=128, unique=True)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='checkouts')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    reservation_id = models.CharField(max_length=128, null=True, blank=True)
    failure_reason = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=['QUEUED', 'PROCESSING', 'CONFIRMED', 'REJECTED', 'FAILED']),
                name='checkout_status_valid',
            ),
        ]

    def __str__(self):
        return f'{self.idempotency_key} ({self.status})'
