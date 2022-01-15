"""
Yield from multiple iterators as values become available

Visit <https://github.com/jwodder/interleave> for more information.
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Queue, SimpleQueue
import sys
from threading import Event, Lock
from types import TracebackType
from typing import (
    Any,
    ContextManager,
    Generic,
    Iterable,
    Iterator,
    Optional,
    Tuple,
    Type,
    TypeVar,
    cast,
)

if sys.version_info[:2] >= (3, 8):
    from typing import Protocol
else:
    from typing_extensions import Protocol

__version__ = "0.1.0.dev1"
__author__ = "John Thorvald Wodder II"
__author_email__ = "interleave@varonathe.org"
__license__ = "MIT"
__url__ = "https://github.com/jwodder/interleave"

__all__ = ["interleave"]

ExcInfo = Tuple[Type[BaseException], BaseException, TracebackType]

T = TypeVar("T")


class Result(Generic[T]):
    def __init__(self, value: Optional[T] = None, exc_info: Optional[ExcInfo] = None):
        self.value = value
        self.exc_info = exc_info

    @property
    def success(self) -> bool:
        return self.exc_info is None

    def get(self) -> T:
        if self.exc_info is None:
            return cast(T, self.value)
        else:
            _, e, tb = self.exc_info
            raise e.with_traceback(tb)

    @classmethod
    def for_exc(cls) -> Result[Any]:
        etype, e, tb = sys.exc_info()
        if etype is None:
            raise ValueError("No exception currently being handled")
        assert etype is not None
        assert e is not None
        assert tb is not None
        return cls(exc_info=(etype, e, tb))


class QueueProto(Protocol, Generic[T]):
    """
    Protocol for the behavior shared by queue.Queue and queue.SimpleQueue that
    is of relevance to this package
    """

    def get(self, block: bool = ..., timeout: Optional[float] = ...) -> T:
        ...

    def put(self, item: T, block: bool = ..., timeout: Optional[float] = ...) -> None:
        ...


class EndOfInputError(Exception):
    pass


class FunnelQueue(Generic[T]):
    def __init__(self, queue_size: Optional[int] = None) -> None:
        self.queue: QueueProto[T]
        if queue_size is None:
            self.queue = SimpleQueue()
        else:
            self.queue = Queue(queue_size)
        self.producer_qty = 0
        self.lock = Lock()
        self.done_sentinel = object()

    def putting(self) -> ContextManager[None]:
        with self.lock:
            self.producer_qty += 1
        return self._close_one()

    @contextmanager
    def _close_one(self) -> Iterator[None]:
        try:
            yield
        finally:
            with self.lock:
                self.producer_qty -= 1
                if self.producer_qty == 0:
                    self.put(cast(T, self.done_sentinel))

    def put(self, value: T) -> None:
        self.queue.put(value)

    def get(self) -> T:
        x = self.queue.get()
        if x is self.done_sentinel:
            raise EndOfInputError()
        else:
            return x


def interleave(
    iterators: Iterable[Iterator[T]],
    *,
    max_workers: Optional[int] = None,
    queue_size: Optional[int] = None,
) -> Iterator[T]:
    funnel: FunnelQueue[Result[T]] = FunnelQueue(queue_size)
    done_flag = Event()

    def process(ctx: ContextManager[None], it: Iterator[T]) -> None:
        with ctx:
            while not done_flag.is_set():
                try:
                    x = next(it)
                except StopIteration:
                    return
                except BaseException:
                    funnel.put(Result.for_exc())
                    return
                else:
                    funnel.put(Result(x))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        # `funnel.putting()` needs to be called outside of the thread so that
        # the producer count is at its correct value at the start, regardless
        # of how many threads are or aren't running yet.  This matters in the
        # case where an initial batch of threads have all finished but there
        # are still pending threads yet to be started; when this happens, we
        # don't want the last thread in the first batch to trigger an
        # EndOfInputError.
        futures = [pool.submit(process, funnel.putting(), it) for it in iterators]
        if not futures:
            return
        while True:
            try:
                r = funnel.get()
            except EndOfInputError:
                break
            else:
                if not r.success:
                    done_flag.set()
                    for f in futures:
                        f.cancel()
                yield r.get()
