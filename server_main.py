import argparse
import asyncio
import os
import secrets

from quota_guard.signaling import SignalServer


async def run(args):
    token = os.environ.get('CQG_RELAY_TOKEN', '')
    if len(token) < 24:
        raise RuntimeError('请设置至少 24 字符的 CQG_RELAY_TOKEN 环境变量；可用 --generate-token 生成')
    server = SignalServer(token)
    async with await server.start(args.host, args.port):
        print(f'CQG discovery/relay listening on {args.host}:{args.port}; use a TLS reverse proxy for public access.', flush=True)
        await asyncio.Future()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Codex Quota Guard encrypted discovery/relay')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=48733)
    parser.add_argument('--generate-token', action='store_true')
    args = parser.parse_args()
    if args.generate_token:
        print(secrets.token_urlsafe(32))
    else:
        try:
            asyncio.run(run(args))
        except KeyboardInterrupt:
            pass
