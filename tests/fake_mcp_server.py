"""A tiny MCP server for the tests: newline-delimited JSON-RPC 2.0 on
stdin/stdout (initialize, tools/list, tools/call, ping). No SDK, no network.

Flags (argv):
  --exit-at-start  write an error (with $FAKE_TOKEN in it) to stderr and exit 3
  --silent         read requests but never answer them
  --chatty         print a non-JSON line and a log notification before each answer
  --ping           ask the client for a ping before answering tools/list
  --page           list the tools over two pages (nextCursor)
  --no-init-check  answer requests sent before initialize
"""
import json
import os
import sys

TOOLS = [
    {"name": "echo", "description": "Repeats the given text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Adds two numbers a and b.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "env", "description": "Says whether an environment variable is set.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object"}},
    {"name": "big", "description": "Returns a very long text.", "inputSchema": {"type": "object"}},
    {"name": "pongs", "description": "How many pings the client answered.", "inputSchema": {"type": "object"}},
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def result(rid, value):
    send({"jsonrpc": "2.0", "id": rid, "result": value})


def text(rid, value, is_error=False):
    result(rid, {"content": [{"type": "text", "text": value}], **({"isError": True} if is_error else {})})


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    flags = set(sys.argv[1:])
    if "--exit-at-start" in flags:
        sys.stderr.write("fake-mcp: cannot start with token " + os.environ.get("FAKE_TOKEN", "") + "\n")
        sys.stderr.flush()
        sys.exit(3)
    initialized = False
    pongs = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if "method" not in message:  # the client's answer to our ping
            pongs += "result" in message
            continue
        if "id" not in message or "--silent" in flags:
            continue  # a notification
        if "--chatty" in flags:
            sys.stdout.write("fake-mcp is thinking...\n")
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "hi"}})
        rid, method, params = message["id"], message["method"], message.get("params") or {}
        if method == "initialize":
            initialized = True
            result(rid, {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                         "serverInfo": {"name": "fake-mcp", "version": "1.2.3"},
                         "instructions": "Use echo to repeat text and add to sum two numbers."})
        elif not initialized and "--no-init-check" not in flags:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "Server not initialized"}})
        elif method == "tools/list":
            if "--ping" in flags:
                send({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
            if "--page" in flags and not params.get("cursor"):
                result(rid, {"tools": TOOLS[:2], "nextCursor": "page-2"})
            elif "--page" in flags:
                result(rid, {"tools": TOOLS[2:]})
            else:
                result(rid, {"tools": TOOLS})
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "echo":
                text(rid, str(args.get("text", "")))
            elif name == "add":
                text(rid, str(args.get("a", 0) + args.get("b", 0)))
            elif name == "env":
                text(rid, "set" if os.environ.get(str(args.get("name", ""))) else "unset")
            elif name == "fail":
                text(rid, "boom: the tool broke", is_error=True)
            elif name == "big":
                text(rid, "x" * 50_000)
            elif name == "pongs":
                text(rid, str(pongs))
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
        elif method == "ping":
            result(rid, {})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})


if __name__ == "__main__":
    main()
