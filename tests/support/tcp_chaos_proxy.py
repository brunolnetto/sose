from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import signal


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


async def _handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    upstream_host: str,
    upstream_port: int,
) -> None:
    try:
        server_reader, server_writer = await asyncio.open_connection(
            upstream_host,
            upstream_port,
        )
    except OSError:
        client_writer.close()
        await client_writer.wait_closed()
        return

    left = asyncio.create_task(_pipe(client_reader, server_writer))
    right = asyncio.create_task(_pipe(server_reader, client_writer))
    done, pending = await asyncio.wait(
        {left, right},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)


async def _main(args) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    server = await asyncio.start_server(
        lambda reader, writer: _handle(
            reader,
            writer,
            upstream_host=args.upstream_host,
            upstream_port=args.upstream_port,
        ),
        args.listen_host,
        args.listen_port,
    )
    args.marker.parent.mkdir(parents=True, exist_ok=True)
    args.marker.write_text("ready", encoding="utf-8")
    try:
        await stop.wait()
    finally:
        server.close()
        await server.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream-host", required=True)
    parser.add_argument("--upstream-port", type=int, required=True)
    parser.add_argument("--marker", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
