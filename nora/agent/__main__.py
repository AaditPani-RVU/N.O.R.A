"""python -m nora.agent pair <url> <code> | run | chat | say <text> | status | rotate"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from nora.agent import client


def _ask_terminal(req: dict) -> bool:
    """A confirmation from the core, answered at this terminal. The prompt is
    the steps themselves (what will run), not the core's summary line."""
    lines = [f"  {s.get('action')} {s.get('params') or {}}" for s in req.get("steps") or []]
    print("\nNORA wants to go ahead with:\n" + "\n".join(lines or [f"  {req.get('rendered', '')}"]))
    try:
        return input("Allow? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


async def _ask_async(req: dict) -> bool:
    return await asyncio.get_running_loop().run_in_executor(None, _ask_terminal, req)


async def _pair(url: str, code: str, name: str, shared: str | None) -> int:
    agent = client.Agent(url, name=name, shared_dir=Path(shared).expanduser() if shared else None)
    reply = await agent.pair(code, platform="laptop")
    if reply.get("type") != "paired":
        print(f"Pairing refused: {reply.get('message') or reply}")
        return 1
    client.save_identity(agent)
    agent.shared_dir.mkdir(parents=True, exist_ok=True)
    print(f"Paired as {agent.device_id}. On the core: python -m nora.hub approve {agent.device_id}")
    print(f"Files you put in {agent.shared_dir} are the only ones NORA can see here.")
    return 0


async def _run(chat: bool) -> int:
    agent = client.from_identity(approve=_ask_async if chat else (lambda _r: False))
    stop = asyncio.Event()
    runner = asyncio.create_task(agent.run_forever(on_state=lambda s: print(f"[{s}]"), stop=stop))
    if not chat:
        await runner
        return 0
    loop = asyncio.get_running_loop()
    print("Type to NORA; an empty line or Ctrl-D quits.")
    try:
        while True:
            text = await loop.run_in_executor(None, _prompt)
            if not text:
                break
            if not agent.connected:
                print("[not connected yet]")
                continue
            try:
                for line in await agent.say(text, timeout=120):
                    print(f"NORA: {line}")
            except asyncio.TimeoutError:
                print("[no answer in two minutes]")
    finally:
        stop.set()
        await runner
    return 0


def _prompt() -> str:
    try:
        return input("> ").strip()
    except EOFError:
        return ""


async def _say(text: str) -> int:
    agent = client.from_identity()
    welcome = await agent.connect()
    if welcome.get("type") != "welcome":
        print(welcome)
        return 1
    try:
        for line in await agent.say(text, timeout=120):
            print(f"NORA: {line}")
    finally:
        await agent.close()
    return 0


async def _rotate() -> int:
    """A fresh key for this laptop; the core forgets the old one."""
    agent = client.from_identity()
    welcome = await agent.connect()
    if welcome.get("type") != "welcome":
        print(welcome)
        return 1
    try:
        reply = await agent.rotate_key()
    finally:
        await agent.close()
    if reply.get("ok") is not True:
        print(f"Not rotated: {reply.get('message')}")
        return 1
    client.save_identity(agent)
    print("New key in use; the old one no longer works.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nora.agent", description="NORA on the travel laptop")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair", help="pair with the core")
    p.add_argument("url", help="wss://<core>.<tailnet>.ts.net/v1/device")
    p.add_argument("code")
    p.add_argument("--name", default="travel laptop")
    p.add_argument("--shared", help="the folder NORA may see (default ~/NORA Shared)")
    sub.add_parser("run", help="stay connected; show NORA's notifications")
    sub.add_parser("chat", help="stay connected and talk to NORA here")
    s = sub.add_parser("say", help="one message, then exit")
    s.add_argument("text")
    sub.add_parser("status", help="what this laptop is paired as")
    sub.add_parser("rotate", help="replace this laptop's key with a new one")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(message)s")
    if args.cmd == "pair":
        return asyncio.run(_pair(args.url, args.code, args.name, args.shared))
    if args.cmd == "status":
        data = client.load_identity()
        print(f"{data.get('name')} = {data['device_id']} at {data['url']}; sharing {data.get('shared_dir')}")
        return 0
    if args.cmd == "rotate":
        return asyncio.run(_rotate())
    if args.cmd == "say":
        return asyncio.run(_say(args.text))
    try:
        return asyncio.run(_run(args.cmd == "chat"))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
