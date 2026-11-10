#!/usr/bin/env python3
"""TCP latency/bandwidth proxy for the dashboard lab.

The research recommends receiver-ingress `tc netem`, which needs root. This is
the user-space substitute: it adds a one-way delay to every chunk in each
direction and optionally rate-limits the stream. It is an approximation, not a
packet-level emulator; the report labels every profile as synthetic.

    proxy.py --listen 19080 --target-host 10.10.10.20 --target-port 18080 \
             --delay-ms 20 --rate-kbps 0
"""
import argparse
import asyncio


async def pipe(reader, writer, delay_ms, rate_kbps):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000.0)
            if rate_kbps:
                # rate is kilobits per second
                await asyncio.sleep(len(data) * 8 / (rate_kbps * 1000.0))
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(client_reader, client_writer, args):
    try:
        server_reader, server_writer = await asyncio.open_connection(
            args.target_host, args.target_port
        )
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(
        pipe(client_reader, server_writer, args.delay_ms, args.rate_kbps),
        pipe(server_reader, client_writer, args.delay_ms, args.rate_kbps),
    )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=int, required=True)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    parser.add_argument("--delay-ms", type=float, default=0.0)
    parser.add_argument("--rate-kbps", type=float, default=0.0)
    args = parser.parse_args()

    server = await asyncio.start_server(
        lambda r, w: handle(r, w, args), "127.0.0.1", args.listen
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
