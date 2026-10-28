#!/usr/bin/env python3
"""Send concurrent, uniquely keyed checkout requests to the configured API."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import statistics
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def send_checkout(api_url, product_id, timeout):
    payload = json.dumps({
        'product_id': product_id,
        'idempotency_key': f'load-test-{uuid4()}',
    }).encode('utf-8')
    request = Request(
        f'{api_url.rstrip("/")}/api/checkout/',
        data=payload,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            status_code = response.status
    except HTTPError as exc:
        status_code = exc.code
        body = exc.read()
    except (URLError, TimeoutError, OSError):
        return 'NETWORK_ERROR', time.perf_counter() - started

    try:
        response_body = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        response_body = None
    return status_code, time.perf_counter() - started, response_body


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8080', help='NGINX base URL')
    parser.add_argument('--product-id', type=int, default=1)
    parser.add_argument('--requests', type=int, default=600)
    parser.add_argument('--concurrency', type=int, default=100)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.timeout <= 0:
        parser.error('requests, concurrency, and timeout must be positive')
    return args


def main():
    args = parse_args()
    results = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(args.concurrency, args.requests)) as executor:
        futures = [
            executor.submit(send_checkout, args.url, args.product_id, args.timeout)
            for _ in range(args.requests)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started

    status_counts = {}
    latencies = []
    for result in results:
        status_code, latency = result[:2]
        status_counts[status_code] = status_counts.get(status_code, 0) + 1
        latencies.append(latency)

    queued = status_counts.get(202, 0)
    rate_limited = status_counts.get(429, 0)
    other_http_errors = sum(
        count for code, count in status_counts.items()
        if isinstance(code, int) and code >= 400 and code != 429
    )
    network_errors = status_counts.get('NETWORK_ERROR', 0)

    print(f'Target: {args.url.rstrip("/")}/api/checkout/')
    print(f'Total requests: {args.requests}')
    print(f'Concurrency: {min(args.concurrency, args.requests)}')
    print(f'Successfully queued (HTTP 202): {queued}')
    print(f'HTTP 429: {rate_limited}')
    print(f'Other HTTP errors: {other_http_errors}')
    print(f'Network/timeout errors: {network_errors}')
    print(f'HTTP status counts: {dict(sorted(status_counts.items(), key=lambda item: str(item[0])))}')
    print(f'Min latency: {min(latencies):.4f} s')
    print(f'Average latency: {statistics.fmean(latencies):.4f} s')
    print(f'Max latency: {max(latencies):.4f} s')
    print(f'Request rate: {len(results) / elapsed:.2f} requests/s')
    print(f'Elapsed time: {elapsed:.2f} s')


if __name__ == '__main__':
    main()
