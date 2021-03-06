import asyncio
import logging
from random import random
from abc import ABC, abstractmethod
from typing import AsyncContextManager
from collections import defaultdict
from .utils import cancel_tasks


log = logging.getLogger(__name__)


class ContextManager(AsyncContextManager):
    __slots__ = "__aenter", "__aexit", "__instance"

    sentinel = object()

    def __init__(self, aenter, aexit):
        self.__aenter = aenter
        self.__aexit = aexit
        self.__instance = self.sentinel

    async def __aenter__(self):
        if self.__instance is not self.sentinel:
            raise RuntimeError("Reuse of context manager is not acceptable")

        self.__instance = await self.__aenter()
        return self.__instance

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.__aexit(self.__instance)


class PoolBase(ABC):
    __slots__ = (
        "_create_lock",
        "_instances",
        "_loop",
        "_recycle",
        "_recycle_bin",
        "_recycle_times",
        "_semaphore",
        "_tasks",
        "_len",
        "_used",
    )

    def __init__(self, maxsize=None, recycle=None):
        assert (
            recycle is None or recycle > 0
        ), "recycle should be positive number or None"

        self._loop = asyncio.get_event_loop()

        self._instances = asyncio.Queue()
        self._recycle_bin = asyncio.Queue()

        self._semaphore = asyncio.Semaphore(maxsize)
        self._len = 0

        self._recycle = recycle

        self._tasks = set()
        self._used = set()

        self._create_lock = asyncio.Lock()
        self._recycle_times = defaultdict(self._loop.time)

        self.__create_task(self.__recycler())

    def __create_task(self, coro):
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.remove)
        return task

    async def __recycler(self):
        while True:
            instance = await self._recycle_bin.get()

            try:
                await self._destroy_instance(instance)
            except Exception:
                log.exception("Error when recycle instance %r", instance)
            finally:
                self._recycle_bin.task_done()

    @abstractmethod
    async def _create_instance(self):
        pass

    @abstractmethod
    async def _destroy_instance(self, instance):
        pass

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    @abstractmethod
    async def _check_instance(self, instance):
        return True

    def __len__(self):
        return self._len

    def __recycle_instance(self, instance):
        self._len -= 1
        self._semaphore.release()
        if instance in self._recycle_times:
            self._recycle_times.pop(instance)

        if instance in self._used:
            self._used.remove(instance)

        self._recycle_bin.put_nowait(instance)

    async def __create_new_instance(self):
        await self._semaphore.acquire()

        instance = await self._create_instance()
        self._len += 1

        if self._recycle:
            deadline = self._recycle * (1 + random())
            self._recycle_times[instance] += deadline

        await self._instances.put(instance)

    async def __acquire(self):
        if not self._semaphore.locked():
            await self.__create_new_instance()

        instance = await self._instances.get()

        try:
            result = await self._check_instance(instance)
        except Exception:
            log.exception("Check instance %r failed", instance)
            self.__recycle_instance(instance)
        else:
            if not result:
                self.__recycle_instance(instance)
                return await self.__acquire()

        self._used.add(instance)
        return instance

    async def __release(self, instance):
        self._used.remove(instance)

        if self._recycle and self._recycle_times[instance] < self._loop.time():
            self.__recycle_instance(instance)
            return

        self._instances.put_nowait(instance)

    def acquire(self):
        return ContextManager(self.__acquire, self.__release)

    async def close(self, timeout=None):
        instances = list(self._used)
        self._used.clear()

        while self._instances.qsize():
            try:
                instances.append(self._instances.get_nowait())
            except asyncio.QueueEmpty:
                break

        async def log_exception(coro):
            try:
                await coro
            except Exception:
                log.exception("Exception when task execution")

        await asyncio.wait_for(
            asyncio.gather(
                *[
                    self.__create_task(
                        log_exception(self._destroy_instance(instance))
                    )
                    for instance in instances
                ],
                return_exceptions=True
            ),
            timeout=timeout,
        )

        await cancel_tasks(self._tasks)
