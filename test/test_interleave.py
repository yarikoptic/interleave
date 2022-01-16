from itertools import count
from math import isclose
import os
from threading import active_count
from time import monotonic, sleep
from typing import Any, Callable, Iterator, List, Optional, Sequence, Tuple, Union
from unittest.mock import MagicMock, call
import pytest
from interleave import FINISH_CURRENT, interleave

UNIT = 0.5


def sleeper(
    tid: int,
    delays: Sequence[Union[int, str]],
    done_callback: Optional[Callable[[int], Any]] = None,
) -> Iterator[Tuple[int, int]]:
    for i, d in enumerate(delays):
        if isinstance(d, int):
            sleep(d * UNIT)
            yield (tid, i)
        else:
            if done_callback is not None:
                done_callback(tid)
            raise RuntimeError(d)
    # We're not putting this under a `finally:` block because (on macOS 11.6
    # Intel with Python 3.9.9 and 3.10.1, at least) doing so would mean it
    # would fire if & when `cancel()` is called on the corresponding future
    # while it's running (in which case a GeneratorExit gets raised in this
    # function), but only if the iterable of iterators passed to `interleave()`
    # was an iterator rather than a list.  I have been unable to write an MVCE
    # that reproduces this behavior, and I'm not sure if it's even worth
    # looking into.
    if done_callback is not None:
        done_callback(tid)


def test_simple() -> None:
    INTERVALS = [
        (0, 1, 2),
        (2, 2, 2),
        (5, 2, 1),
    ]
    threads = active_count()
    cb = MagicMock()
    assert list(
        interleave(sleeper(i, intervals, cb) for i, intervals in enumerate(INTERVALS))
    ) == [
        (0, 0),
        (0, 1),
        (1, 0),
        (0, 2),
        (1, 1),
        (2, 0),
        (1, 2),
        (2, 1),
        (2, 2),
    ]
    assert active_count() == threads
    assert cb.call_args_list == [call(i) for i in range(len(INTERVALS))]


@pytest.mark.flaky(reruns=5, condition="CI" in os.environ)
def test_timing() -> None:
    INTERVALS = [
        (0, 1, 2),
        (2, 2, 2),
        (5, 2, 1),
    ]
    prev: Optional[float] = None
    for _ in interleave(sleeper(i, intervals) for i, intervals in enumerate(INTERVALS)):
        now = monotonic()
        if prev is not None:
            assert isclose(now - prev, UNIT, rel_tol=0.3, abs_tol=0.1)
        prev = now


def test_no_iterators() -> None:
    it: Iterator[Any] = interleave([])
    with pytest.raises(StopIteration):
        next(it)


def test_ragged() -> None:
    INTERVALS = [
        (0, 1, 2, 3, 3),
        (2, 2, 3),
        (5, 3),
    ]
    assert list(
        interleave(sleeper(i, intervals) for i, intervals in enumerate(INTERVALS))
    ) == [
        (0, 0),
        (0, 1),
        (1, 0),
        (0, 2),
        (1, 1),
        (2, 0),
        (0, 3),
        (1, 2),
        (2, 1),
        (0, 4),
    ]


def test_shrinking_ragged() -> None:
    INTERVALS = [
        (0, 1, 2, 3),
        (2, 2, 3),
        (5, 3),
        (9,),
    ]
    assert list(
        interleave(sleeper(i, intervals) for i, intervals in enumerate(INTERVALS))
    ) == [
        (0, 0),
        (0, 1),
        (1, 0),
        (0, 2),
        (1, 1),
        (2, 0),
        (0, 3),
        (1, 2),
        (2, 1),
        (3, 0),
    ]


def test_growing_ragged() -> None:
    INTERVALS = [
        (0,),
        (1, 1),
        (3, 1, 2),
        (5, 2, 1, 1),
    ]
    assert list(
        interleave(sleeper(i, intervals) for i, intervals in enumerate(INTERVALS))
    ) == [
        (0, 0),
        (1, 0),
        (1, 1),
        (2, 0),
        (2, 1),
        (3, 0),
        (2, 2),
        (3, 1),
        (3, 2),
        (3, 3),
    ]


def test_error() -> None:
    INTERVALS: List[Tuple[Union[int, str], ...]] = [
        (0, 1, 2),
        (2, 2, "This is an error.", "This is not raised."),
        (5, "This is not seen.", 1),
    ]
    threads = active_count()
    cb = MagicMock()
    it = interleave(sleeper(i, intervals, cb) for i, intervals in enumerate(INTERVALS))
    for expected in [(0, 0), (0, 1), (1, 0), (0, 2), (1, 1)]:
        assert next(it) == expected
    with pytest.raises(RuntimeError) as excinfo:
        next(it)
    assert str(excinfo.value) == "This is an error."
    assert active_count() == threads
    assert cb.call_args_list == [call(0), call(1)]


def test_error_sized_queue() -> None:
    INTERVALS: List[Tuple[Union[int, str], ...]] = [
        (0, 1, 2),
        (2, 2, "This is an error.", "This is not raised."),
        (5, "This is not seen.", 1),
    ]

    def queue_spam(tid: int) -> Iterator[Tuple[int, int]]:
        sleep(6 * UNIT)
        for i in count():
            yield (tid, i)

    threads = active_count()
    cb = MagicMock()
    it = interleave(
        [sleeper(i, intervals, cb) for i, intervals in enumerate(INTERVALS)]
        + [queue_spam(len(INTERVALS))],
        queue_size=4,
    )
    for expected in [(0, 0), (0, 1), (1, 0), (0, 2), (1, 1)]:
        assert next(it) == expected
    with pytest.raises(RuntimeError) as excinfo:
        next(it)
    assert str(excinfo.value) == "This is an error."
    assert active_count() == threads
    assert cb.call_args_list == [call(0), call(1)]


def test_finish_current() -> None:
    INTERVALS: List[Tuple[Union[int, str], ...]] = [
        (0, 1, 2),
        (2, 2, "This is an error."),
        (5, 1, 3),
        (8, "This error is discarded."),
    ]
    threads = active_count()
    cb = MagicMock()
    it = interleave(
        [sleeper(i, intervals, cb) for i, intervals in enumerate(INTERVALS)],
        onerror=FINISH_CURRENT,
    )
    for expected in [
        (0, 0),
        (0, 1),
        (1, 0),
        (0, 2),
        (1, 1),
        (2, 0),
        (2, 1),
        (3, 0),
        (2, 2),
    ]:
        assert next(it) == expected
    with pytest.raises(RuntimeError) as excinfo:
        next(it)
    assert str(excinfo.value) == "This is an error."
    assert active_count() == threads
    assert cb.call_args_list == [call(0), call(1), call(3), call(2)]


def test_max_workers() -> None:
    INTERVALS = [
        (0, 1, 2, 3),
        (2, 2),
        (5, 3, 3),
        (9, 3),
        (3, 3),
    ]
    cb = MagicMock()
    assert list(
        interleave(
            [sleeper(i, intervals, cb) for i, intervals in enumerate(INTERVALS)],
            max_workers=4,
        )
    ) == [
        (0, 0),
        (0, 1),
        (1, 0),
        (0, 2),
        (1, 1),
        (2, 0),
        (0, 3),
        (4, 0),
        (2, 1),
        (3, 0),
        (4, 1),
        (2, 2),
        (3, 1),
    ]
    assert cb.call_args_list == [call(1), call(0), call(4), call(2), call(3)]
