import logging

from django.conf import settings
from django.db import IntegrityError, connections, transaction
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Checkout, Product
from .serializers import CheckoutRequestSerializer, CheckoutStatusSerializer, ProductSerializer
from .services.inventory import (
    InventoryError,
    InventoryNotInitialized,
    InventoryUnavailable,
    release_reservation,
    reserve_inventory,
)
from .services.rate_limit import allow_checkout_request
from .tasks import process_checkout


logger = logging.getLogger(__name__)


class ProductListView(generics.ListAPIView):
    queryset = Product.objects.all().order_by('id')
    serializer_class = ProductSerializer


class CheckoutCreateView(APIView):
    def post(self, request):
        trusted_ip_header = settings.CHECKOUT_RATE_LIMIT_CLIENT_IP_HEADER
        client_ip = (
            request.META.get(trusted_ip_header) if trusted_ip_header else None
        ) or request.META.get('REMOTE_ADDR') or 'unknown'
        allowed, retry_after = allow_checkout_request(client_ip)
        if not allowed:
            response = Response(
                {
                    'error': 'rate_limited',
                    'detail': 'Too many checkout requests. Please try again shortly.',
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            response['Retry-After'] = str(retry_after)
            return response

        serializer = CheckoutRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product_id = serializer.validated_data['product_id']
        idempotency_key = serializer.validated_data['idempotency_key']

        try:
            with transaction.atomic():
                checkout = Checkout.objects.create(
                    idempotency_key=idempotency_key,
                    product_id=product_id,
                    status=Checkout.Status.QUEUED,
                )
        except IntegrityError:
            # The unique insert is the concurrency-safe idempotency check and also
            # lets the foreign key validate product_id without a separate SELECT.
            existing = Checkout.objects.filter(idempotency_key=idempotency_key).first()
            if existing:
                return Response(CheckoutStatusSerializer(existing).data, status=status.HTTP_200_OK)
            if not Product.objects.filter(pk=product_id).exists():
                return Response(
                    {'error': 'product_not_found', 'detail': 'Product does not exist.'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            raise

        # The Redis reservation must not keep this request's PostgreSQL connection open.
        connections.close_all()

        try:
            reservation = reserve_inventory(product_id)
        except InventoryNotInitialized:
            self._mark_failed(checkout, 'Redis inventory has not been initialized.')
            return Response(
                {'error': 'inventory_not_initialized', 'detail': 'Sale inventory is not ready.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except InventoryUnavailable:
            self._mark_failed(checkout, 'Redis reservation could not be completed.')
            return Response(
                {'error': 'inventory_unavailable', 'detail': 'Sale inventory is temporarily unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if not reservation.accepted:
            checkout.status = Checkout.Status.REJECTED
            checkout.failure_reason = 'No inventory remains for this sale.'
            checkout.save(update_fields=['status', 'failure_reason', 'updated_at'])
            return Response(
                {'checkout_id': checkout.pk, 'status': checkout.status, 'error': 'sold_out'},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            with transaction.atomic():
                checkout.reservation_id = reservation.reservation_id
                checkout.save(update_fields=['reservation_id', 'updated_at'])
                transaction.on_commit(lambda: self._enqueue_checkout(checkout.pk))
        except Exception:
            logger.exception('Could not enqueue checkout %s.', checkout.pk)
            self._fail_queued_checkout(checkout, reservation.reservation_id)
            checkout.refresh_from_db()
            return Response(
                {
                    'checkout_id': checkout.pk,
                    'status': checkout.status,
                    'error': 'checkout_queue_unavailable',
                    'detail': 'Checkout could not be queued. Check its status before retrying.',
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {'checkout_id': checkout.pk, 'status': Checkout.Status.QUEUED},
            status=status.HTTP_202_ACCEPTED,
        )

    @staticmethod
    def _mark_failed(checkout, reason):
        checkout.status = Checkout.Status.FAILED
        checkout.failure_reason = reason
        checkout.save(update_fields=['status', 'failure_reason', 'updated_at'])

    @staticmethod
    def _enqueue_checkout(checkout_id):
        # on_commit runs after the row is durable; close DB before broker I/O.
        connections.close_all()
        process_checkout.apply_async(args=[checkout_id])

    @staticmethod
    def _fail_queued_checkout(checkout, reservation_id):
        try:
            failed = Checkout.objects.filter(
                pk=checkout.pk,
                status=Checkout.Status.QUEUED,
            ).update(
                status=Checkout.Status.FAILED,
                failure_reason='Checkout could not be queued and will not be processed.',
                updated_at=timezone.now(),
            )
        except Exception:
            logger.exception('Could not mark checkout %s as failed.', checkout.pk)
            return

        # If a worker already claimed the task, its PostgreSQL transaction owns the reservation.
        if failed:
            connections.close_all()
            try:
                release_reservation(checkout.product_id, reservation_id)
            except InventoryError:
                logger.exception('Could not release Redis reservation for checkout %s.', checkout.pk)


class CheckoutStatusView(generics.RetrieveAPIView):
    queryset = Checkout.objects.select_related('product')
    serializer_class = CheckoutStatusSerializer
    lookup_field = 'pk'
    lookup_url_kwarg = 'checkout_id'
