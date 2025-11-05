import asyncio

from fastapi_ws_rpc.config import WebSocketRpcClientConfig
from fastapi_ws_rpc.rpc_methods import RpcUtilityMethods
from fastapi_ws_rpc.websocket_rpc_client import WebSocketRpcClient


async def run_client(uri):
    # Configure additional headers via websocket_kwargs
    config = WebSocketRpcClientConfig(
        websocket_kwargs={
            "additional_headers": [("X-TOKEN", "fake-super-secret-token")]
        }
    )
    async with WebSocketRpcClient(uri, RpcUtilityMethods(), config=config) as client:
        # call echo on the other side (using ".other" syntactic sugar)
        response = await client.other.echo(text="Hello World!")
        print(response)
        # call get_process_details on the other side
        response = await client.call("get_process_details")
        print(response)


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(run_client("ws://localhost:8000/ws/a3"))
