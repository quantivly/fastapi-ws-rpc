<p align="center">
<img src="https://i.ibb.co/m8jL6Zd/RPC.png" alt="RPC" border="0" width="50%" />
</p>

#

# ⚡ FASTAPI Websocket RPC
RPC over Websockets made easy, robust, and production ready

<a href="https://github.com/permitio/fastapi_websocket_rpc/actions?query=workflow%3ATests" target="_blank">
    <img src="https://github.com/permitio/fastapi_websocket_rpc/workflows/Tests/badge.svg" alt="Tests">
</a>

<a href="https://pypi.org/project/fastapi-websocket-rpc/" target="_blank">
    <img src="https://img.shields.io/pypi/v/fastapi-websocket-rpc?color=%2331C654&label=PyPi%20package" alt="Package">
</a>

<a href="https://pepy.tech/project/fastapi-websocket-rpc" target="_blank">
    <img src="https://static.pepy.tech/personalized-badge/fastapi-websocket-rpc?period=total&units=international_system&left_color=black&right_color=blue&left_text=Downloads" alt="Downloads">
</a>

A fast and durable bidirectional JSON RPC channel over Websockets.
The easiest way to create a live async channel between two nodes via Python (or other clients).

- Both server and clients can easily expose Python methods that can be called by the other side.
Method return values are sent back as RPC responses, which the other side can wait on.
- Remote methods are easily called via the ```.other.method()``` wrapper
- Connections are kept alive with a configurable retry mechanism  (using Tenacity)

- As seen at <a href="https://www.youtube.com/watch?v=KP7tPeKhT3o" target="_blank">PyCon IL 2021</a> and <a href="https://www.youtube.com/watch?v=IuMZVWEUvGs" target="_blank">EuroPython 2021</a>


Supports and tested on Python >= 3.9
## Installation 🛠️
```
pip install fastapi_ws_rpc
```


## RPC call example:

Say the server exposes an "add" method, e.g. :
```python
class RpcCalculator(RpcMethodsBase):
    async def add(self, a, b):
        return a + b
```
Calling it is as easy as calling the method under the client's "other" property:
```python
response = await client.other.add(a=1,b=2)
print(response.result) # 3
```
getting the response with the return value.




## Usage example:

### Server:

```python
import uvicorn
from fastapi import FastAPI
from fastapi_ws_rpc import RpcMethodsBase, WebSocketRpcEndpoint


# Methods to expose to the clients
class ConcatServer(RpcMethodsBase):
    async def concat(self, a="", b=""):
        return a + b


# Init the FAST-API app
app = FastAPI()
# Create an endpoint and load it with the methods to expose
endpoint = WebSocketRpcEndpoint(ConcatServer())
# add the endpoint to the app
endpoint.register_route(app, "/ws")

# Start the server itself
uvicorn.run(app, host="0.0.0.0", port=9000)
```
### Client

```python
import asyncio
from fastapi_ws_rpc import RpcMethodsBase, WebSocketRpcClient


async def run_client(uri):
    async with WebSocketRpcClient(uri, RpcMethodsBase()) as client:
        # call concat on the other side
        response = await client.other.concat(a="hello", b=" world")
        # print result
        print(response.result)  # will print "hello world"


# run the client until it completes interaction with server
asyncio.get_event_loop().run_until_complete(
  run_client("ws://localhost:9000/ws")
)
```

See the [examples](/examples) and [tests](/tests) folders for more server and client examples


## Server calling client example:
- Clients can call ```client.other.method()```
    - which is a shortcut for ```channel.other.method()```
- Servers also get the channel object and can call remote methods via ```channel.other.method()```
- See the [bidirectional call example](examples/bidirectional_server_example.py) for calling client from server and server events (e.g. ```on_connect```).


## What can I do with this?
Websockets are ideal to create bi-directional realtime connections over the web.
 - Push updates
 - Remote control mechanism
 - Pub / Sub (see [fastapi_websocket_pubsub](https://github.com/permitio/fastapi_websocket_pubsub))
 - Trigger events (see "tests/trigger_flow_test.py")
 - Node negotiations (see "tests/advanced_rpc_test.py :: test_recursive_rpc_calls")

## Production Features 🚀

**NEW in v1.0.0**: Built-in production hardening features:

- **Message Size Limits** - Prevent DoS attacks via extremely large JSON payloads (default 10MB limit, configurable)
- **Rate Limiting** - Control request flooding with max pending requests (default 1000, configurable)
- **Connection Duration Limits** - Automatically close long-lived connections after max age
- **Enhanced Keepalive** - Consecutive ping failure detection with auto-reconnect (3 failures → close)
- **State Validation** - Proper connection state checking with meaningful error messages
- **JSON-RPC 2.0 Compliance** - Full standard error codes and method validation
- **Notifications** - Fire-and-forget messaging with `notify()` method

### Using Production Features

```python
from fastapi_ws_rpc import WebSocketRpcEndpoint, WebSocketRpcClient

# Server with production hardening
endpoint = WebSocketRpcEndpoint(
    methods,
    max_message_size=5 * 1024 * 1024,  # 5MB limit
    max_pending_requests=500,          # Limit concurrent requests
    max_connection_duration=3600.0,    # Close after 1 hour
)

# Client with enhanced keepalive
client = WebSocketRpcClient(
    uri,
    methods,
    keep_alive=5.0,                        # Ping every 5 seconds
    max_consecutive_ping_failures=3,       # Auto-close after 3 failures
)

# Send fire-and-forget notifications
await channel.notify("log_event", {"level": "info", "message": "Processing..."})
```

### Production Deployment Guide

**1. Certificate Validation**

The library now validates certificate expiry before establishing connections. Ensure your certificates are valid:

```python
# Client will automatically validate certificate expiry and reject expired certs
ssl_context = tls_service.get_ssl_context()  # Validates cert is not expired
client = WebSocketRpcClient(uri, methods, ssl=ssl_context)
```

**2. Connection Health Monitoring**

Use protocol-level ping/pong (RFC 6455) for reliable health checking:

```python
from fastapi_ws_rpc import RpcKeepaliveConfig

config = WebSocketRpcClientConfig(
    keepalive=RpcKeepaliveConfig(
        interval=30.0,              # Ping every 30 seconds
        use_protocol_ping=True,     # Use WebSocket protocol ping (recommended)
        max_failures=3,             # Close after 3 consecutive failures
    ),
    websocket_kwargs={
        "ping_interval": 30,        # WebSocket library ping interval
        "ping_timeout": 20,         # Pong response timeout
    }
)
```

**3. Memory Management**

Promise cleanup has been optimized in v1.0.0:

- **Immediate cleanup** - Timed-out promises cleaned up immediately (not after 60s)
- **Reduced TTL** - Promise TTL reduced from 300s to 60s
- **Automatic cleanup** - Periodic cleanup runs every 60s for edge cases

**4. Failure Modes**

**Scenario: Server Unavailable**
- Client attempts reconnection with exponential backoff
- Max 10 attempts with up to 60s delay between attempts
- Jitter (0-25%) prevents thundering herd

**Scenario: Network Partition**
- Keepalive detects connection loss within 30-60s (2x ping interval)
- Client triggers automatic reconnection
- Pending requests raise `RpcChannelClosedError`

**Scenario: Slow Server (Slowloris Attack)**
- 60-second message receive timeout protects client
- Connection closed if message takes >60s to receive
- Prevents resource exhaustion from slow-send attacks

**Scenario: Memory Pressure**
- Backpressure limits enforced (1000 pending requests default)
- New requests rejected with `RpcBackpressureError` when limit reached
- System self-protects against request flooding

**5. Security Best Practices**

- ✅ **Always use WSS** - Never use ws:// in production
- ✅ **Validate certificates** - Automatically done in v1.0.0
- ✅ **Set message size limits** - Protect against DoS (10MB default)
- ✅ **Enable keepalive** - Detect dead connections quickly
- ✅ **Implement timeouts** - Use `default_response_timeout` config
- ✅ **Monitor error rates** - Track `RpcChannelClosedError`, `RpcBackpressureError`

**6. Performance Tuning**

```python
# High-throughput configuration
config = WebSocketRpcClientConfig(
    backpressure=RpcBackpressureConfig(
        max_pending_requests=2000,      # Allow more concurrent requests
        max_send_queue_size=2000,       # Larger send buffer
    ),
    connection=RpcConnectionConfig(
        default_response_timeout=10.0,  # Faster timeouts for low-latency
    )
)

# Long-running operations configuration
config = WebSocketRpcClientConfig(
    connection=RpcConnectionConfig(
        default_response_timeout=300.0,  # 5 minutes for slow operations
        max_connection_duration=86400.0, # 24 hours max connection age
    )
)
```

## Concepts
- [RpcChannel](fastapi_ws_rpc/rpc_channel.py) - implements the RPC-protocol over the websocket
    - Sending RpcRequests per method call
    - Creating promises to track them (via unique call ids), and allow waiting for responses
    - Executing methods on the remote side and serializing return values as
    - Receiving RpcResponses and delivering them to waiting callers
- [RpcMethods](fastapi_ws_rpc/rpc_methods.py) - classes passed to both client and server-endpoint inits to expose callable methods to the other side.
    - Simply derive from RpcMethodsBase and add your own async methods
    - Note currently only key-word arguments are supported
    - Checkout RpcUtilityMethods for example methods, which are also useful debugging utilities


- Foundations:

    - Based on [asyncio](https://docs.python.org/3/library/asyncio.html) for the power of Python coroutines

    - Server Endpoint:
        - Based on [FAST-API](https://github.com/tiangolo/fastapi): enjoy all the benefits of a full ASGI platform, including Async-io and dependency injections (for example to authenticate connections)

        - Based on [Pydantic](https://pydantic-docs.helpmanual.io/): easily serialize structured data as part of RPC requests and responses (see 'tests/basic_rpc_test.py :: test_structured_response' for an example)

    - Client :
        - Based on [Tenacity](https://tenacity.readthedocs.io/en/latest/index.html): allowing configurable retries to keep to connection alive
            - see WebSocketRpcClient.__init__'s retry_config
        - Based on python [websockets](https://websockets.readthedocs.io/en/stable/intro.html) - a more comprehensive client than the one offered by Fast-api

## Logging
fastapi-websocket-rpc provides a helper logging module to control how it produces logs for you.
See [fastapi_ws_rpc/logger.py](fastapi_ws_rpc/logger.py).
Use ```logging_config.set_mode``` or the 'WS_RPC_LOGGING' environment variable to choose the logging method you prefer or override completely via default logging config.

example:

```python
# set RPC to log like UVICORN
from fastapi_ws_rpc.logger import logging_config, LoggingModes

logging_config.set_mode(LoggingModes.UVICORN)
```

## Development

### Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for:
- Development setup instructions
- Code style guidelines
- Testing requirements
- Pull request process

### Changelog

See [CHANGELOG.md](CHANGELOG.md) for a detailed history of changes.

**Note**: Version 1.0.0 includes breaking changes:
- **Class renamed**: `WebsocketRPCEndpoint` → `WebSocketRpcEndpoint` (note the capitalization)
- **Minimum Python version**: Now requires Python 3.9+ (dropped 3.7 and 3.8)
- **Exception hierarchy**: `RpcChannelClosedError` and `RemoteValueError` now inherit from `RpcError`
- **Removed**: `requirements.txt` (use `pyproject.toml` for dependencies)
- See [CHANGELOG.md](CHANGELOG.md) for complete migration details

### Known Limitations

#### JSON-RPC 2.0 Batch Requests

Batch requests (arrays of request objects) are not currently supported in v1.0.0.
This feature is planned for v1.1.0.

**Workaround:** Send individual requests sequentially or use `asyncio.gather()` for concurrent execution.

```python
# Instead of batch request:
# await channel.call_batch([...])

# Use concurrent individual calls:
results = await asyncio.gather(
    channel.call("method1", {"a": 1}),
    channel.call("method2", {"b": 2}),
)
```

See [Issue #10](https://github.com/permitio/fastapi_websocket_rpc/issues) for implementation tracking.

## Pull Requests Welcome! 🎉

- Please include tests for new features
- Follow the code style guidelines in [CONTRIBUTING.md](CONTRIBUTING.md)
- Update documentation as needed
