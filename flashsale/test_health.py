import redis
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIClient


class HealthEndpointTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_endpoint_reports_application_health(self):
        response = self.client.get('/health/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    @patch('flashsale.health.celery_app.connection_for_write')
    @patch('flashsale.health.get_redis_client')
    @patch('flashsale.health.connections')
    def test_readiness_reports_required_dependencies(self, connections, get_redis_client, broker_factory):
        cursor = connections.__getitem__.return_value.cursor.return_value.__enter__.return_value
        broker = Mock()
        broker_factory.return_value = broker

        response = self.client.get('/ready/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'status': 'ready',
            'checks': {'database': 'ok', 'redis': 'ok', 'rabbitmq': 'ok'},
        })
        cursor.execute.assert_called_once_with('SELECT 1')
        get_redis_client.return_value.ping.assert_called_once()
        broker.ensure_connection.assert_called_once_with(max_retries=0)
        broker.release.assert_called_once()
        connections.close_all.assert_called_once()

    @patch('flashsale.health.celery_app.connection_for_write')
    @patch('flashsale.health.get_redis_client')
    @patch('flashsale.health.connections')
    def test_readiness_returns_503_when_a_dependency_is_down(
        self, connections, get_redis_client, broker_factory
    ):
        connections.__getitem__.return_value.cursor.side_effect = RuntimeError('database unavailable')
        get_redis_client.return_value.ping.side_effect = redis.ConnectionError('redis unavailable')
        broker_factory.return_value.ensure_connection.side_effect = OSError('broker unavailable')

        response = self.client.get('/ready/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'not_ready')
        self.assertEqual(response.json()['checks'], {
            'database': 'unavailable',
            'redis': 'unavailable',
            'rabbitmq': 'unavailable',
        })
