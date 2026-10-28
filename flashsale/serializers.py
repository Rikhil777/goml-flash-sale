from rest_framework import serializers

from .models import Checkout, Product


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ('id', 'name', 'description', 'price', 'total_stock', 'available_stock')
        read_only_fields = fields


class CheckoutRequestSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128, allow_blank=False, trim_whitespace=True)


class CheckoutStatusSerializer(serializers.ModelSerializer):
    checkout_id = serializers.IntegerField(source='id', read_only=True)
    product_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Checkout
        fields = ('checkout_id', 'product_id', 'status', 'failure_reason')
        read_only_fields = fields
