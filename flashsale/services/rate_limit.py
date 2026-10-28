from hashlib import sha256
import logging

import redis
from django.conf import settings

from .inventory import get_redis_client


logger = logging.getLogger(__name__)

CHECKOUT_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
end

local retry_after = redis.call('TTL', KEYS[1])
if count > tonumber(ARGV[1]) then
    return {0, retry_after}
end
return {1, retry_after}
"""


def checkout_rate_limit_key(client_ip):
    identity = sha256(client_ip.encode('utf-8')).hexdigest()
    return f'flashsale:rate-limit:checkout:{identity}'


def allow_checkout_request(client_ip):
    """Return (allowed, retry_after); fail open if Redis cannot rate-limit."""
    limit = settings.CHECKOUT_RATE_LIMIT_REQUESTS
    window = settings.CHECKOUT_RATE_LIMIT_WINDOW
    key = checkout_rate_limit_key(client_ip)

    try:
        allowed, retry_after = get_redis_client().eval(
            CHECKOUT_RATE_LIMIT_SCRIPT,
            1,
            key,
            limit,
            window,
        )
        return bool(int(allowed)), max(1, int(retry_after))
    except redis.RedisError:
        logger.warning('Redis rate limiter unavailable; allowing checkout request.')
        return True, None
