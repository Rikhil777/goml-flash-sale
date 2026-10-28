from django.contrib import admin

from .models import Checkout, Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'price', 'total_stock', 'available_stock', 'updated_at')
    search_fields = ('name',)


@admin.register(Checkout)
class CheckoutAdmin(admin.ModelAdmin):
    list_display = ('idempotency_key', 'product', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('idempotency_key', 'reservation_id')
    readonly_fields = ('created_at', 'updated_at')
