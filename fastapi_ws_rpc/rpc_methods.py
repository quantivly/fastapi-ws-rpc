from __future__ import annotations

import asyncio
import copy
import os
import sys
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .utils import gen_uid

if TYPE_CHECKING:
    from .rpc_channel import RpcChannel

PING_RESPONSE = "pong"
# list of internal methods that can be called from remote
EXPOSED_BUILT_IN_METHODS = ["ping", "_get_channel_id_"]
# NULL default value - indicating no response was received


class NoResponse:
    pass


class RpcMethodsBase:
    """
    The basic interface RPC channels expects method groups to implement.
     - create copy of the method object
     - set channel
     - provide 'ping' for keep-alive
    """

    def __init__(self) -> None:
        self._channel: RpcChannel | None = None

    def _set_channel_(self, channel: RpcChannel) -> None:
        """
        Allows the channel to share access to its functions to the methods once
        nested under it
        """
        self._channel = channel

    @property
    def channel(self) -> RpcChannel | None:
        return self._channel

    def _copy_(self) -> RpcMethodsBase:
        """Simple copy ctor - overriding classes may need to override copy as well."""
        return copy.copy(self)

    async def ping(self) -> str:
        """
        Built-in ping for connection verification and keep-alive.
        """
        return PING_RESPONSE

    async def _get_channel_id_(self) -> str:
        """
        built in channel id to better identify your remote
        """
        if self._channel is None:
            raise RuntimeError("Channel not initialized")
        return self._channel.id


class ProcessDetails(BaseModel):
    pid: int = os.getpid()
    cmd: list[str] = sys.argv
    workingdir: str = os.getcwd()


class RpcUtilityMethods(RpcMethodsBase):
    """
    A simple set of RPC functions useful for management and testing
    """

    def __init__(self) -> None:
        """
        endpoint (WebsocketRPCEndpoint): the endpoint these methods are loaded into
        """
        super().__init__()

    async def get_process_details(self) -> ProcessDetails:
        return ProcessDetails()

    async def call_me_back(
        self, method_name: str = "", args: dict[str, Any] | None = None
    ) -> str:
        if args is None:
            args = {}
        if self.channel is not None:
            # generate a uid we can use to track this request
            call_id = gen_uid()
            # Call async -  without waiting to avoid locking the event_loop
            asyncio.create_task(
                self.channel.async_call(method_name, args=args, call_id=call_id)
            )
            # return the id- which can be used to check the response once it's received
            return call_id
        return ""

    async def get_response(self, call_id: str = "") -> Any:
        if self.channel is not None:
            res = self.channel.get_saved_response(call_id)
            self.channel.clear_saved_call(call_id)
            return res
        return None

    async def echo(self, text: str) -> str:
        return text
