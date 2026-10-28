from django.urls import path

from .views import CheckoutCreateView, CheckoutStatusView, ProductListView


urlpatterns = [
    path('products/', ProductListView.as_view(), name='product-list'),
    path('checkout/', CheckoutCreateView.as_view(), name='checkout-create'),
    path('checkout/<int:checkout_id>/', CheckoutStatusView.as_view(), name='checkout-status'),
]
