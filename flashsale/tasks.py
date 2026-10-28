import logging

from celery import shared_task
from django.db import transaction
from django.db.models import F

from .models import Checkout, Product


logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=2)
def process_checkout(self, checkout_id):
    """Persist one Redis reservation to PostgreSQL, safely across redeliveries."""
    try:
        with transaction.atomic():
            try:
                checkout = Checkout.objects.select_for_update().get(pk=checkout_id)
            except Checkout.DoesNotExist:
                logger.warning('Checkout %s no longer exists.', checkout_id)
                return 'MISSING'

            if checkout.status in (
                Checkout.Status.CONFIRMED,
                Checkout.Status.REJECTED,
                Checkout.Status.FAILED,
            ):
                return checkout.status
            if checkout.status != Checkout.Status.QUEUED:
                return checkout.status

            checkout.status = Checkout.Status.PROCESSING
            checkout.save(update_fields=['status', 'updated_at'])

            updated = Product.objects.filter(
                pk=checkout.product_id,
                available_stock__gt=0,
            ).update(available_stock=F('available_stock') - 1)

            if updated == 1:
                checkout.status = Checkout.Status.CONFIRMED
                checkout.failure_reason = None
            else:
                checkout.status = Checkout.Status.REJECTED
                checkout.failure_reason = 'No PostgreSQL inventory remained when checkout was processed.'
            checkout.save(update_fields=['status', 'failure_reason', 'updated_at'])
            return checkout.status
    except Exception as exc:
        logger.exception('Checkout %s processing failed.', checkout_id)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)

        # The transaction rolled back, so a failed worker has not consumed PostgreSQL stock.
        Checkout.objects.filter(pk=checkout_id, status=Checkout.Status.QUEUED).update(
            status=Checkout.Status.FAILED,
            failure_reason='Checkout processing failed after retries.',
        )
        return Checkout.Status.FAILED
