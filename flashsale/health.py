from django.db import connections
from django.http import JsonResponse
import redis

from config.celery import app as celery_app
from .services.inventory import get_redis_client


def health_check(_request):
    return JsonResponse({'status': 'ok'})


def readiness_check(_request):
    checks = {}

    try:
        with connections['default'].cursor() as cursor:
            cursor.execute('SELECT 1')
        checks['database'] = 'ok'
    except Exception:
        checks['database'] = 'unavailable'
    finally:
        connections.close_all()

    try:
        get_redis_client().ping()
        checks['redis'] = 'ok'
    except redis.RedisError:
        checks['redis'] = 'unavailable'

    broker_connection = None
    try:
        broker_connection = celery_app.connection_for_write()
        broker_connection.ensure_connection(max_retries=0)
        checks['rabbitmq'] = 'ok'
    except Exception:
        checks['rabbitmq'] = 'unavailable'
    finally:
        if broker_connection is not None:
            try:
                broker_connection.release()
            except Exception:
                checks['rabbitmq'] = 'unavailable'

    ready = all(value == 'ok' for value in checks.values())
    return JsonResponse(
        {'status': 'ready' if ready else 'not_ready', 'checks': checks},
        status=200 if ready else 503,
    )
