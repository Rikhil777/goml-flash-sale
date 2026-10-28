# goML flash sale

## Phase 3 checkout flow

`POST /api/checkout/` accepts a `product_id` and a unique `idempotency_key`. Django first stores a queued checkout, reserves one Redis inventory token with the existing Lua script, and publishes `process_checkout(checkout_id)` to Celery. It returns `202 Accepted` after the task is published. Repeating an idempotency key returns the existing checkout and does not reserve or publish again.

For burst traffic, PostgreSQL `CONN_MAX_AGE` defaults to `0` (override with `POSTGRES_CONN_MAX_AGE`). The request closes its database connection before Redis reservation and again before RabbitMQ publication, so neither network operation holds a PostgreSQL client slot.

The Celery worker locks the checkout row and performs the PostgreSQL inventory decrement and checkout status change in one database transaction. It uses a conditional update (`available_stock > 0`) with `available_stock = available_stock - 1`; PostgreSQL therefore cannot decrement inventory below zero. Duplicate task deliveries see the terminal checkout state and do not decrement again. `GET /api/checkout/<checkout_id>/` returns the current status.

Redis is the fast reservation gate; PostgreSQL remains the persistent record. If PostgreSQL has no stock when a queued reservation is processed, the checkout is rejected and Redis is not incremented, since its count may already be stale. Reconcile Redis from PostgreSQL deliberately with `python manage.py sync_redis_inventory --product-id <id> --force` after reviewing outstanding checkouts.

The database, Redis, and RabbitMQ do not share a transaction. If RabbitMQ publishing reports an error, the API marks a still-queued checkout failed and releases its Redis reservation atomically. If a worker has already claimed the checkout, the API does not release it. A Redis outage during that release can leave a token unavailable until Redis is synchronized; this reduces sales but cannot oversell. There is also an unavoidable brief window between the Redis reservation and durable task publication.

## Rate limiting and health endpoints

`POST /api/checkout/` uses an atomic Redis fixed-window counter keyed by a hash of the client IP. When Django is accessed directly, that address comes from `REMOTE_ADDR`. In the Docker Compose path, trusted NGINX passes the client address in `X-Real-IP`, which Django is configured to use because NGINX is the trusted reverse proxy; arbitrary client-supplied `X-Real-IP` headers are not trusted when Django is directly exposed. The default is 100 requests per 60 seconds per client IP. Requests over the limit receive HTTP `429 Too Many Requests` and a `Retry-After` header with the remaining window in seconds. Product and checkout-status GET endpoints are not rate-limited. When Redis is unavailable, the limiter fails open so it does not add another outage dependency; inventory reservation still independently requires Redis and fails safely if inventory cannot be reserved.

Configure the limit with `CHECKOUT_RATE_LIMIT_REQUESTS` and `CHECKOUT_RATE_LIMIT_WINDOW` (seconds). When using another reverse proxy, configure Django to trust its client-IP header only when that proxy is trusted and prevents clients from spoofing it.

`GET /health/` returns HTTP 200 when Django can serve requests. `GET /ready/` checks PostgreSQL, Redis, and RabbitMQ and returns 503 with component states if a checkout dependency is unavailable. Readiness is an operator/deployment probe and is not called from the checkout endpoint.

Run the tests with `python manage.py test`. The rate-limit and health coverage can also be run directly with `python manage.py test flashsale.test_rate_limit flashsale.test_health`.

## Local checkout services

Install project dependencies with `python -m pip install -r requirements.txt`. Start PostgreSQL and Redis as in the earlier setup, initialize the product and Redis inventory, and start RabbitMQ. The broker URL defaults to `amqp://guest:guest@localhost:5672//`; set `CELERY_BROKER_URL` for another broker or credentials.

Run the web application and worker in separate terminals:

```powershell
python manage.py runserver
celery -A config worker --loglevel=INFO --pool=solo
```

The `solo` worker pool is suitable for local Windows development. Use a process pool on platforms that support it for production deployments.

## Frontend flow

The Django template page loads product details from `GET /api/products/`, then sends one `POST /api/checkout/` request when the buyer selects **Buy now**:

```text
Browser -> Product API -> Checkout POST -> Redis reservation
        -> RabbitMQ -> Celery worker -> PostgreSQL transaction
        -> checkout status polling
```

The button is disabled while an attempt is being submitted or polled, preventing double-clicks and overlapping checkout flows. Each new attempt gets a fresh browser-generated idempotency key. If the POST response is ambiguous because of a network/server error, retrying reuses that attempt's key so the backend can return the existing checkout instead of creating another one. The page polls the status endpoint only after it has a checkout ID, and stops polling at a terminal state.

The page displays product price and stock from the API and refreshes them after confirmation or rejection. JavaScript never decrements the stock itself; Redis and PostgreSQL remain authoritative for reservation and durable inventory updates. The interface uses local vanilla JavaScript and CSS without external frameworks, fonts, or images.

## Production-style local architecture

```text
Browser
   |
   v
 NGINX
   |
   v
Gunicorn / Django
   |             \
   v              v
 Redis         RabbitMQ
inventory gate     |
                   v
              Celery worker
                   |
                   v
              PostgreSQL
```

- **NGINX** is the only published application port. It serves collected static files, proxies the page and API to Gunicorn, and applies an IP-based checkout `limit_req` rule.
- **Gunicorn/Django** runs stateless web workers. It keeps checkout state in PostgreSQL/Redis/RabbitMQ rather than process memory. PostgreSQL `CONN_MAX_AGE=0` remains in place, and the checkout path closes the DB connection before Redis and RabbitMQ I/O.
- **Redis** is the fast atomic inventory gate and also backs Django's application-level checkout rate limiter.
- **RabbitMQ/Celery** moves checkout processing out of the request path. The API returns `202` after publishing; the worker performs the durable update.
- **PostgreSQL** remains the persistent source of truth. The worker conditionally decrements stock in a transaction and cannot decrement below zero.

Redis is used before PostgreSQL because a Lua reservation can quickly admit at most the available tokens without making every request contend on one database inventory row. PostgreSQL is still necessary to durably record checkout outcomes and perform the final transactional stock update. The unique idempotency key makes retries of the same request return the same checkout instead of reserving again.

NGINX rate limiting protects the gateway from bursts; Django's Redis-backed limit is a second application-level control. The Compose NGINX template overwrites `X-Real-IP`, and Compose configures the application to use that trusted header for its per-client limiter. Do not enable that setting when Django can be reached directly by untrusted clients.

## Why a naive implementation fails

A naive handler might read stock, check it in Python, then update the row and create an order:

```text
POST -> SELECT stock -> if stock > 0 -> UPDATE stock -> create order
```

Under concurrency, many requests can read the same positive value before any update commits, then all try to sell that unit. Locking one PostgreSQL inventory row for every request prevents that race, but turns the row into a serialization point and a throughput bottleneck during a flash sale.

This design uses a fast Redis atomic reservation, then an asynchronous RabbitMQ/Celery queue, followed by PostgreSQL's transactional conditional decrement. Redis reduces contention on the hot admission path; PostgreSQL remains the final durable guard against overselling. The local load test validates this invariant under a modest concurrent burst; it does not establish capacity for 100,000 users.

## Docker Compose demo

The Compose stack publishes only NGINX on host port 8080. PostgreSQL, Redis, RabbitMQ, Gunicorn, and Celery use the Compose network and service names. PostgreSQL, Redis, RabbitMQ, and collected static files use named volumes. The PostgreSQL container uses its internal port 5432 and does not connect to or publish the machine's native PostgreSQL on 5432 or the development container mapped to 5433.

Copy `.env.example` to `.env`, replace the placeholders, then validate and build before starting services:

```powershell
Copy-Item .env.example .env
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

The local-only fallback credentials in Compose are for demonstration; set private values in `.env` for local use and never use those defaults in a deployed environment. The web container applies migrations, collects static files, and starts Gunicorn. It waits for healthy PostgreSQL, Redis, and RabbitMQ. The worker waits for the web health check, which becomes healthy after migrations and Gunicorn start. NGINX waits for the web health check.

Initialize the sale once in the fresh Compose database and copy its stock into the fresh Redis volume:

```powershell
docker compose exec web python manage.py initialize_sale
docker compose exec web python manage.py sync_redis_inventory --product-id 1
```

Then verify the page and service endpoints through NGINX:

```powershell
curl.exe -i http://127.0.0.1:8080/
curl.exe -i http://127.0.0.1:8080/health/
curl.exe -i http://127.0.0.1:8080/ready/
curl.exe -i http://127.0.0.1:8080/api/products/
```

`/health/` checks the Django process; `/ready/` checks PostgreSQL, Redis, and RabbitMQ. Compose also has container health checks for those services and for Django.

## Controlled concurrency demonstration

The load script uses Python's standard library and gives every POST a fresh UUID idempotency key. It reports request counts by HTTP status, latency, and achieved requests per second:

```powershell
python scripts/load_test.py --url http://127.0.0.1:8080 --product-id 1 --requests 600 --concurrency 100
```

For a test focused on inventory correctness, raise both configured limits enough that the test mostly measures inventory admission instead of rate limiting. Set these in `.env` before `docker compose up -d --force-recreate web nginx`:

```dotenv
CHECKOUT_RATE_LIMIT_REQUESTS=1000
CHECKOUT_RATE_LIMIT_WINDOW=60
NGINX_CHECKOUT_RATE=1000r/s
NGINX_CHECKOUT_BURST=1000
```

Use the isolated Phase 6 Compose database and Redis volume, not the existing development database. On a fresh stack, the initializer creates 500 stock with no previous checkouts, then the Redis sync initializes 500 tokens. Allow the worker queue to drain after the run before checking totals. For a repeated run, reset only this disposable Compose dataset, after confirming no prior jobs remain:

```powershell
docker compose exec web python manage.py shell -c "from flashsale.models import Checkout, Product; Product.objects.filter(pk=1).update(available_stock=500); Checkout.objects.all().delete()"
docker compose exec web python manage.py sync_redis_inventory --product-id 1 --force
```

After the worker finishes, verify database stock and checkout outcomes:

```powershell
docker compose exec -T web python manage.py shell -c "from flashsale.models import Checkout, Product; p=Product.objects.get(pk=1); print({'available_stock': p.available_stock, 'confirmed': Checkout.objects.filter(status='CONFIRMED').count(), 'rejected': Checkout.objects.filter(status='REJECTED').count(), 'queued_or_processing': Checkout.objects.filter(status__in=['QUEUED','PROCESSING']).count()})"
docker compose exec -T redis redis-cli GET flashsale:inventory:1
```

For this fresh test dataset, the invariant is `available_stock >= 0` and `confirmed <= 500`; Redis inventory must also be nonnegative. A 600-request run can validate that no more than 500 are confirmed, but it is not a 100,000-concurrent-user capacity test. The script reports measured HTTP metrics only; confirmed/rejected totals come from the PostgreSQL query above.

The gateway used for the load test is NGINX -> Gunicorn -> Django, not Django's development server. `GUNICORN_WORKERS` defaults to 4 and `CELERY_WORKER_CONCURRENCY` defaults to 2; tune these conservatively against the PostgreSQL connection budget. No connection pool or larger PostgreSQL `max_connections` is configured.

## Render deployment

The root `render.yaml` defines a Django web service, Render PostgreSQL, Render Key Value (Redis-compatible), a private RabbitMQ service with a persistent disk, and a Celery background worker. The browser frontend remains a Django template served by the web service. Redis remains the atomic inventory reservation service; Celery continues to publish through RabbitMQ.

### Deploy the Blueprint

1. Push this repository to the Git provider connected to Render. Do not add a `.env` file or real credentials to Git.
2. In Render, create a Blueprint instance from this repository and select `render.yaml`.
3. Review the resource region and plans in `render.yaml`. The services are set to Singapore; the PostgreSQL database, private RabbitMQ service, web service, worker, and Key Value must remain in the same region for private connectivity. The database plan and persistent RabbitMQ disk require paid Render resources.
4. Sync the Blueprint. Render generates the Django secret key and RabbitMQ password in environment groups and links each application's services to PostgreSQL, Redis, and RabbitMQ over Render's private network.
5. Wait for the web service and worker to become healthy. Render runs `python manage.py migrate --noinput` as the web service's pre-deploy command. The web container runs `python manage.py collectstatic --noinput` before starting Gunicorn; those files are served by WhiteNoise from that same container.
6. Add `flashsale.rikhilkakani.online` as a custom domain on the Django web service in Render. Render's dashboard will show the DNS target and verification status.

`/health/` is configured as Render's web health check and checks that Django can serve a request. `/ready/` remains available to check PostgreSQL, Redis, and RabbitMQ together.

### Render environment variables

The Blueprint creates and connects the service variables below. Secret values are generated by Render and must not be copied into this repository.

| Variable | Services | Source / purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | Web, worker | Render-generated secret from `flashsale-app-secrets`. Required in production. |
| `DJANGO_ENV` | Web, worker | `production`; activates production-safe Django settings. |
| `DJANGO_DEBUG` | Web, worker | `false`; production also forces debug off. |
| `DJANGO_ALLOWED_HOSTS` | Web, worker | `flashsale.rikhilkakani.online,.onrender.com`; editable for the selected Render hostname. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Web | HTTPS origins for the custom domain and Render subdomains. |
| `DJANGO_SECURE_HSTS_SECONDS` | Web, worker | `31536000`, supplied by the app secrets group. |
| `DATABASE_URL` | Web, worker | Internal PostgreSQL connection string linked from Render Postgres. |
| `POSTGRES_CONN_MAX_AGE` | Web, worker | `0`, preserving short-lived PostgreSQL connections. |
| `REDIS_URL` | Web, worker | Private Redis-compatible Key Value connection string. Used for inventory reservations and request limiting. |
| `RABBITMQ_HOST` | Web, worker | Private hostname linked from the RabbitMQ service. |
| `RABBITMQ_PORT` | Web, worker | `5672`, RabbitMQ's AMQP port. |
| `RABBITMQ_DEFAULT_USER` | RabbitMQ, web, worker | `flashsale_app`, shared from the credentials group. |
| `RABBITMQ_DEFAULT_PASS` | RabbitMQ, web, worker | Render-generated secret from `rabbitmq-credentials`. |
| `CELERY_BROKER_URL` | Web, worker | Optional override. If unset, Django constructs the AMQP URL from the RabbitMQ private host, port, and shared credentials. It never points Celery at Redis. |
| `GUNICORN_WORKERS` | Web | `2`; adjust to the web service plan. |
| `CHECKOUT_RATE_LIMIT_REQUESTS` | Web | `100`. |
| `CHECKOUT_RATE_LIMIT_WINDOW` | Web | `60` seconds. |
| `CHECKOUT_RATE_LIMIT_CLIENT_IP_HEADER` | Web | Empty because Django is directly exposed behind Render's managed proxy and does not trust a client-supplied IP header. |
| `CELERY_WORKER_CONCURRENCY` | Worker | `2`; the Blueprint's worker command uses concurrency 2. |

`DATABASE_URL`, `REDIS_URL`, and `RABBITMQ_HOST` are linked values, so they should not be manually replaced with public connection addresses. If a secret is rotated, update the RabbitMQ service and its shared Render environment group together, then redeploy the web and worker.

### Custom subdomain DNS

Only add the `flashsale` subdomain. Do not add `rikhilkakani.online` or change any existing root-domain or portfolio DNS records.

1. In the Render web service, open **Settings → Custom Domains** and add `flashsale.rikhilkakani.online`.
2. At the DNS provider that hosts `rikhilkakani.online`, create the CNAME record Render displays: host/name `flashsale`, target the service's assigned `*.onrender.com` hostname (or the exact target Render presents), and use the TTL recommended by the provider.
3. Remove only a conflicting existing record for the `flashsale` host, if one exists. Leave the apex/root and all portfolio records unchanged.
4. Return to Render and wait for DNS verification and TLS certificate provisioning. Then verify `https://flashsale.rikhilkakani.online/`, `/health/`, `/ready/`, and `/api/products/`.
