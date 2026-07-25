import argparse
import json
import os
import sys
import time
import requests
from utils import get_proxy_candidates


def configure_output_encoding():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def load_proxy_source_config():
    try:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {
            'proxy_source': data.get('proxy_source', 'file'),
            'proxy_file': data.get('proxy_file', 'proxies.txt'),
        }
    except Exception:
        return {
            'proxy_source': 'file',
            'proxy_file': 'proxies.txt',
        }


def mask_proxy(proxy_config):
    if proxy_config['username']:
        return f"{proxy_config['server']}:{proxy_config['username']}:***"
    return proxy_config['server']


def check_single_url(proxy_config, url, timeout):
    start = time.perf_counter()
    try:
        response = requests.get(
            url,
            proxies={
                'http': proxy_config['requests_url'],
                'https': proxy_config['requests_url'],
            },
            timeout=(timeout, timeout),
            allow_redirects=True,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'
            }
        )
        elapsed = time.perf_counter() - start
        return {
            'ok': True,
            'status_code': response.status_code,
            'elapsed': elapsed,
            'final_url': response.url,
            'content_length': len(response.text),
            'error': None,
        }
    except Exception as e:
        elapsed = time.perf_counter() - start
        return {
            'ok': False,
            'status_code': None,
            'elapsed': elapsed,
            'final_url': None,
            'content_length': 0,
            'error': str(e),
        }


def main():
    configure_output_encoding()
    source_config = load_proxy_source_config()

    parser = argparse.ArgumentParser(description='Test proxies from file or proxy API')
    parser.add_argument('--file', default=source_config['proxy_file'], help='proxy file path')
    parser.add_argument(
        '--source',
        choices=['file', 'freefile', 'api'],
        default=source_config['proxy_source'],
        help='proxy source',
    )
    parser.add_argument('--timeout', type=int, default=15, help='read timeout in seconds')
    parser.add_argument('--limit', type=int, default=1, help='how many proxies to test from the source')
    parser.add_argument(
        '--url',
        action='append',
        dest='urls',
        help='target url to test, can be passed multiple times'
    )
    args = parser.parse_args()

    urls = args.urls or [
        'https://ipinfo.io/json',
        'https://www.microsoft.com',
        'https://signup.live.com',
        'https://login.live.com',
        'https://outlook.live.com/mail/0/?prompt=create_account',
    ]

    proxies = get_proxy_candidates(
        args.file if args.source == 'file' else None,
        proxy_source=args.source,
    )
    if not proxies:
        print(f'未读取到代理，来源: {args.source}', flush=True)
        return

    if args.limit > 0:
        proxies = proxies[:args.limit]

    total = len(proxies)
    passed = 0

    print(f'开始测试，共 {total} 条代理，来源 {args.source}，目标 {len(urls)} 个，读取超时 {args.timeout}s', flush=True)
    print('-' * 80, flush=True)

    for index, proxy_config in enumerate(proxies, start=1):
        print(f'[{index}/{total}] {mask_proxy(proxy_config)}', flush=True)
        proxy_ok = True

        for url in urls:
            print(f'  TEST {url}', flush=True)
            result = check_single_url(proxy_config, url, args.timeout)
            if result['ok']:
                print(
                    f"  OK   {url} | status={result['status_code']} | "
                    f"time={result['elapsed']:.2f}s | len={result['content_length']} | final={result['final_url']}",
                    flush=True,
                )
            else:
                proxy_ok = False
                print(
                    f"  FAIL {url} | time={result['elapsed']:.2f}s | error={result['error']}",
                    flush=True,
                )

        if proxy_ok:
            passed += 1
        print('-' * 80, flush=True)

    print(f'测试完成，可用 {passed}/{total}', flush=True)


if __name__ == '__main__':
    main()
