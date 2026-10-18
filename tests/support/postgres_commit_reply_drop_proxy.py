"""Test-only PostgreSQL protocol proxy: withhold successful COMMIT response.

The worker arms this relay only after its *real* SQL certificate INSERT and
Command DELETE have been sent, while the outer transaction is still open.
The relay then forwards the next frontend COMMIT, witnesses backend
CommandComplete("COMMIT") AND ReadyForQuery, and drops that response on the
floor before the client can read it. This reproduces unknown commit outcome
for the caller with known committed truth established separately by SQL.

This is strictly a non-production fault injector. The caller must require
the exact drop marker, nonzero worker return and canonical recovery evidence.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import signal


def _query_has_commit(data: bytes) -> bool:
    # Both PostgreSQL simple Query (Q) and extended Parse (P) carry the
    # literal NUL-terminated statement. A rolling window covers TCP splits.
    return b"COMMIT\x00" in data


async def _handle(client_reader, client_writer, *, args):
    try:
        if args.upstream_unix_socket_dir:
            server_reader, server_writer = await asyncio.open_unix_connection(
                f"{args.upstream_unix_socket_dir.rstrip('/')}/.s.PGSQL.{args.upstream_port}"
            )
        else:
            server_reader, server_writer = await asyncio.open_connection(
                args.upstream_host, args.upstream_port
            )
    except OSError:
        client_writer.close()
        await client_writer.wait_closed()
        return

    commit_forwarded = asyncio.Event()

    async def client_to_server():
        tail = b""
        try:
            while data := await client_reader.read(65536):
                if args.arm.exists() and _query_has_commit(tail + data):
                    # Claim the one-shot trigger before forwarding COMMIT.
                    # Other connections must not accidentally intercept an
                    # earlier/future transaction from the other organization.
                    try:
                        args.arm.rename(args.claimed)
                    except FileNotFoundError:
                        pass
                    else:
                        commit_forwarded.set()
                server_writer.write(data)
                await server_writer.drain()
                tail = (tail + data)[-64:]
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def server_to_client():
        buffered = bytearray()
        saw_command_complete = False
        try:
            while data := await server_reader.read(65536):
                if not commit_forwarded.is_set():
                    client_writer.write(data)
                    await client_writer.drain()
                    continue
                # Do not forward even a byte of the COMMIT reply. Parse the
                # backend protocol messages only once the single target
                # frontend COMMIT has been forwarded to PostgreSQL.
                buffered.extend(data)
                while len(buffered) >= 5:
                    message_size = int.from_bytes(buffered[1:5], "big")
                    if not 4 <= message_size <= 16_777_216:
                        raise ValueError("invalid PostgreSQL backend message length")
                    total_size = 1 + message_size
                    if len(buffered) < total_size:
                        break
                    kind = bytes(buffered[:1])
                    payload = bytes(buffered[5:total_size])
                    del buffered[:total_size]
                    if kind == b"C" and payload == b"COMMIT\x00":
                        saw_command_complete = True
                    elif kind == b"E":
                        args.dropped.write_text("server_error_before_commit")
                        return
                    elif kind == b"Z":
                        if not saw_command_complete:
                            args.dropped.write_text("ready_without_commit_complete")
                        else:
                            # The server processed COMMIT successfully, but
                            # the caller has seen neither C nor Z.
                            args.dropped.write_text("commit_complete_response_dropped")
                        return
        except (ConnectionError, asyncio.CancelledError):
            pass

    left = asyncio.create_task(client_to_server())
    right = asyncio.create_task(server_to_client())
    done, pending = await asyncio.wait(
        (left, right), return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)
    # Abort the client transport instead of gracefully relaying a successful
    # COMMIT response. psycopg must report an uncertain connection failure.
    client_writer.transport.abort()
    server_writer.close()
    try:
        await server_writer.wait_closed()
    except ConnectionError:
        pass


async def _run(args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    server = await asyncio.start_server(
        lambda reader, writer: _handle(reader, writer, args=args),
        "127.0.0.1", args.listen_port,
    )
    args.ready.parent.mkdir(parents=True, exist_ok=True)
    args.ready.write_text("ready")
    try:
        await stop.wait()
    finally:
        server.close()
        await server.wait_closed()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream-host")
    parser.add_argument("--upstream-unix-socket-dir")
    parser.add_argument("--upstream-port", type=int, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--arm", type=Path, required=True)
    parser.add_argument("--claimed", type=Path, required=True)
    parser.add_argument("--dropped", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.upstream_host) == bool(args.upstream_unix_socket_dir):
        parser.error("exactly one upstream address required")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
