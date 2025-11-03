from __future__ import annotations

import datetime
import os
import re
import uuid
from datetime import timedelta
from random import SystemRandom, randrange
from typing import TYPE_CHECKING, Any, TypeVar

import pydantic
from packaging import version

from .logger import get_logger

if TYPE_CHECKING:
    from pydantic import BaseModel

T = TypeVar("T", bound="BaseModel")

logger = get_logger("fastapi_ws_rpc.utils")


class RandomUtils:
    @staticmethod
    def gen_cookie_id() -> str:
        return os.urandom(16).hex()

    @staticmethod
    def gen_uid() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def gen_token(size: int = 256) -> str:
        if size % 2 != 0:
            raise ValueError("Size in bits must be an even number.")
        return (
            uuid.UUID(int=SystemRandom().getrandbits(size // 2)).hex
            + uuid.UUID(int=SystemRandom().getrandbits(size // 2)).hex
        )

    @staticmethod
    def random_datetime(
        start: datetime.datetime | None = None, end: datetime.datetime | None = None
    ) -> datetime.datetime:
        """
        This function will return a random datetime between two datetime
        objects.
        If no range is provided - a last 24-hours range will be used
        :param datetime,None start: start date for range, now if None
        :param datetime,None end: end date for range, next 24-hours if None
        """
        # build range
        now = datetime.datetime.now()
        start = start or now
        end = end or now + timedelta(hours=24)
        delta = end - start
        int_delta = (delta.days * 24 * 60 * 60) + delta.seconds
        # Random time
        random_second = randrange(int_delta)
        # Return random date
        return start + timedelta(seconds=random_second)


gen_uid = RandomUtils.gen_uid
gen_token = RandomUtils.gen_token


class StringUtils:
    @staticmethod
    def convert_camelcase_to_underscore(name: str, lower: bool = True) -> str:
        s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
        res = re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1)
        if lower:
            return res.lower()
        else:
            return res.upper()


# Helper methods for supporting Pydantic v1 and v2
def is_pydantic_pre_v2() -> bool:
    return version.parse(pydantic.VERSION) < version.parse("2.0.0")


def pydantic_serialize(model: BaseModel, **kwargs: Any) -> str:
    if is_pydantic_pre_v2():
        return model.json(**kwargs)
    return model.model_dump_json(**kwargs)


def pydantic_parse(model: type[T], data: dict[str, Any], **kwargs: Any) -> T:
    logger.debug(f"Using pydantic to parse: {data}")
    if is_pydantic_pre_v2():
        parsed_data = model.parse_obj(data, **kwargs)
    else:
        parsed_data = model.model_validate(data, **kwargs)
    logger.debug(f"Pydantic parsed data: {parsed_data}")
    return parsed_data
