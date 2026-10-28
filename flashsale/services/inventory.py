from dataclasses import dataclass
from uuid import uuid4

import redis
from django.conf import settings
from flashsale.models import Product


RESERVE_INVENTORY_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
    return {-1, -1}
end

local stock = tonumber(current)
if stock <= 0 then
    return {0, 0}
end

if redis.call('EXISTS', KEYS[2]) == 1 then
    return {-2, -1}
end

local remaining = redis.call('DECR', KEYS[1])
redis.call('SET', KEYS[2], 'reserved')
return {1, remaining}
"""


RELEASE_INVENTORY_SCRIPT = """
local reservation_state = redis.call('GET', KEYS[2])
if reservation_state == 'released' then
    return {0, tonumber(redis.call('GET', KEYS[1])) or -1}
end
if reservation_state ~= 'reserved' then
    return {-1, -1}
end
if not redis.call('GET', KEYS[1]) then
    return {-2, -1}
end

local remaining = redis.call('INCR', KEYS[1])
redis.call('SET', KEYS[2], 'released')
return {1, remaining}
"""


class InventoryError(Exception):
    """Base error for Redis inventory operations."""


class InventoryUnavailable(InventoryError):
    """Redis could not complete an inventory operation."""


class InventoryNotInitialized(InventoryError):
    """No Redis inventory value has been initialized for this product."""


class ReservationNotFound(InventoryError):
    """The Redis reservation identifier is unknown or no longer valid."""


@dataclass(frozen=True)
class ReservationResult:
    accepted: bool
    reservation_id: str | None
    remaining_stock: int | None


def inventory_key(product_id):
    return f'flashsale:inventory:{product_id}'


def reservation_key(reservation_id):
    return f'flashsale:reservation:{reservation_id}'


def get_redis_client():
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=2,
    )


def initialize_inventory(product_id, force=False):
    """Copy PostgreSQL stock to Redis; preserve an existing value unless forced."""
    product = Product.objects.get(pk=product_id)

    try:
        initialized = get_redis_client().set(
            inventory_key(product.pk), product.available_stock, nx=not force
        )
    except redis.RedisError as exc:
        raise InventoryUnavailable('Redis is unavailable; inventory was not synchronized.') from exc
    return bool(initialized)


def reserve_inventory(product_id):
    """Atomically reserve one Redis inventory unit without changing PostgreSQL."""
    reservation_id = str(uuid4())
    try:
        result = get_redis_client().eval(
            RESERVE_INVENTORY_SCRIPT,
            2,
            inventory_key(product_id),
            reservation_key(reservation_id),
        )
    except redis.RedisError as exc:
        raise InventoryUnavailable('Redis is unavailable; no reservation was made.') from exc

    status, remaining = (int(value) for value in result)
    if status == -1:
        raise InventoryNotInitialized(f'Redis inventory for product {product_id} is not initialized.')
    if status == 0:
        return ReservationResult(accepted=False, reservation_id=None, remaining_stock=remaining)
    if status == -2:
        raise InventoryUnavailable('Redis could not create a unique reservation.')
    if status != 1 or remaining < 0:
        raise InventoryUnavailable('Redis returned an invalid inventory reservation result.')

    return ReservationResult(accepted=True, reservation_id=reservation_id, remaining_stock=remaining)


def release_reservation(product_id, reservation_id):
    """Restore a reservation once, using Redis Lua to prevent duplicate releases."""
    try:
        result = get_redis_client().eval(
            RELEASE_INVENTORY_SCRIPT,
            2,
            inventory_key(product_id),
            reservation_key(reservation_id),
        )
    except redis.RedisError as exc:
        raise InventoryUnavailable('Redis is unavailable; reservation was not released.') from exc

    status = int(result[0])
    if status == 1:
        return True
    if status == 0:
        return False
    if status == -2:
        raise InventoryNotInitialized(f'Redis inventory for product {product_id} is not initialized.')
    raise ReservationNotFound(f'Reservation {reservation_id} is not active.')
