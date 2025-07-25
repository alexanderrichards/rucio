# Copyright European Organization for Nuclear Research (CERN) since 2012
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import logging
import os
import types
from typing import cast, get_type_hints

import pytest

from rucio.common.bittorrent import bittorrent_v2_merkle_sha256
from rucio.common.exception import InvalidType
from rucio.common.logging import formatted_logger
from rucio.common.utils import Availability, clone_function, parse_did_filter_from_string, retrying


class TestUtils:
    """UTILS (COMMON): test utility functions"""

    def test_parse_did_filter_string(self):
        """(COMMON/UTILS): test parsing of DID filter string"""
        test_cases = [{
            'input': 'type=all,length=3,length>4,length>=6,length<=7,  test=b, created_after=1900-01-01T00:00:00.000Z',
            'expected_filter': {'length': 3, 'length.gt': 4, 'length.gte': 6, 'length.lte': 7, 'test': 'b',
                                'created_after': datetime.datetime.strptime('1900-01-01T00:00:00.000Z',
                                                                            '%Y-%m-%dT%H:%M:%S.%fZ')},
            'expected_type': 'all'
        }, {
            'input': 'type=FILE',
            'expected_filter': {},
            'expected_type': 'file'
        }, {
            'input': '',
            'expected_filter': {},
            'expected_type': 'collection'
        }]

        for test_case in test_cases:
            filters, type_ = parse_did_filter_from_string(test_case['input'])
            assert test_case['expected_filter'] == filters
            assert test_case['expected_type'] == type_

        with pytest.raises(InvalidType):
            input_ = 'type=g'
            parse_did_filter_from_string(input_)

    def test_availability_data_class(self):
        availability = Availability()

        assert availability.read is None
        assert availability.write is None
        assert availability.delete is None

        availability = Availability(True, False, True)

        assert availability.read
        assert not availability.write
        assert availability.delete

    def test_availability_tuple_unpacking(self):
        read, write, delete = Availability(True, False, True)

        assert read
        assert not write
        assert delete

    def test_availability_hash(self):
        hash(Availability(True, True, True))

    def test_availability_with_none(self):
        assert Availability(write=False).integer == 5

    def test_availability_from_integer_none(self):
        assert Availability.from_integer(None) == Availability(None, None, None)


@pytest.mark.parametrize(
    "integer,tuple_values",
    [
        #   (read,  write, delete)
        (7, (None, None, None)),
        (6, (True, None, False)),
        (5, (None, False, None)),
    ],
)
def test_availability_convert_with_none(integer, tuple_values):
    """
    This tests the conversion to an integer with missing values. Missing values
    should be interpreted as `True`, since this is the default value.
    """
    assert integer == Availability(*tuple_values).integer


@pytest.mark.parametrize(
    "before,after",
    [
        #   (read,  write, delete)
        (7, (True, True, True)),
        (6, (True, True, False)),
        (5, (True, False, True)),
        (4, (True, False, False)),
        (3, (False, True, True)),
        (2, (False, True, False)),
        (1, (False, False, True)),
        (0, (False, False, False)),
    ],
)
def test_availability_translation(before, after):
    assert Availability.from_integer(before) == Availability(*after)
    assert tuple(Availability.from_integer(before)) == after

    assert Availability.from_integer(before).integer == before
    assert Availability(*after).integer == before


def test_formatted_logger():
    result = None

    def log_func(level, msg, *args, **kwargs):
        nonlocal result
        result = (level, msg)

    new_log_func = formatted_logger(log_func, "a %s c")

    new_log_func(logging.INFO, "b")
    assert result == (logging.INFO, "a b c")


def test_retrying():
    attempts = []
    start_time = datetime.datetime.now()

    @retrying(retry_on_exception=lambda _: True, wait_fixed=550, stop_max_attempt_number=3)
    def always_retry():
        attempts.append(True)
        raise ValueError()

    with pytest.raises(ValueError):
        always_retry()
    assert len(attempts) == 3
    assert datetime.datetime.now() - start_time > datetime.timedelta(seconds=1)

    attempts = []

    @retrying(retry_on_exception=lambda e: isinstance(e, AttributeError), wait_fixed=1, stop_max_attempt_number=3)
    def retry_on_attribute_error():
        attempts.append(True)
        raise ValueError()

    with pytest.raises(ValueError):
        retry_on_attribute_error()
    assert len(attempts) == 1


def test_bittorrent_sa256_merkle(file_factory):
    def _sha256_merkle_via_libtorrent(file, piece_size=0):
        import libtorrent as lt
        file = str(file)
        fs = lt.file_storage()
        lt.add_files(fs, file)
        t = lt.create_torrent(fs, flags=lt.create_torrent.v2_only, piece_size=piece_size)
        lt.set_piece_hashes(t, os.path.dirname(file))

        torrent = t.generate()
        pieces_root = next(iter(next(iter(torrent[b'info'][b'file tree'].values())).values()))[b'pieces root']
        pieces_layers = torrent.get(b'piece layers', {}).get(pieces_root, b'')
        piece_size = t.piece_length()

        return pieces_root, pieces_layers, piece_size

    for size in (1, 333, 1024, 16384, 16390, 32768, 32769, 49152, 65530, 65536, 81920, 2 ** 20 - 2 ** 17, 2 ** 20,
                 2 ** 20 + 2):
        file = file_factory.file_generator(size=size)
        root, layers, piece_size = bittorrent_v2_merkle_sha256(file)
        assert (root, layers, piece_size) == _sha256_merkle_via_libtorrent(file, piece_size=piece_size)


# A sample callable that deliberately includes:
#   * positional parameter with a default (b)
#   * keyword‑only parameter with a default (k)
#   * return‑type annotation
# so that the tests can verify preservation of defaults and annotations.
def _sample(a: int, b: int = 3, *, k: str = "x") -> str:
    """Example for clone_function tests."""
    return f"{a + b}-{k}"


@pytest.mark.parametrize(
    "args, kwargs, expected",
    [
        #  (args)   (kwargs)   expected‑return‑value
        ((1,),      {},        "4-x"),   # uses all defaults
        ((2, 5),    {"k": "y"}, "7-y"),  # overrides both defaults
    ],
)
def test_clone_function_behaves_like_original(args, kwargs, expected):
    """
    An *independent* clone must:
    • be a **different** object instance (otherwise attribute mutations would leak),
    • yield the **same result** for every valid call signature.
    """
    clone = clone_function(_sample)

    assert clone is not _sample
    assert isinstance(clone, types.FunctionType)
    assert clone(*args, **kwargs) == _sample(*args, **kwargs) == expected


def test_clone_function_attribute_independence():
    """
    After cloning we tweak *only* the clone’s attributes; the original must
    stay intact, demonstrating that the helper made a *shallow copy* of metadata
    instead of aliasing it.
    """
    clone = clone_function(_sample)

    # mutate some attrs + inject a custom attr
    clone.__doc__ = "mutated"
    clone.__name__ = "mutated_name"
    clone.flag = True

    assert _sample.__doc__ == "Example for clone_function tests."
    assert _sample.__name__ == "_sample"
    assert not hasattr(_sample, "flag")


def test_clone_function_metadata_copied():
    """
    Core metadata and annotations are faithfully copied.
    """
    clone_ft = cast('types.FunctionType', clone_function(_sample))
    sample_ft = cast('types.FunctionType', _sample)

    # Positional‑arg defaults and kw‑only defaults must match by value
    assert clone_ft.__defaults__ == sample_ft.__defaults__
    assert clone_ft.__kwdefaults__ == sample_ft.__kwdefaults__

    # update_wrapper keeps the qualified name
    # Check the suffix since the module path may differ
    assert clone_ft.__qualname__.endswith("_sample")

    # Typing information should also match exactly
    assert get_type_hints(clone_ft) == get_type_hints(sample_ft)

    # functools.update_wrapper() (called inside clone_function) normally leaves a __wrapped__
    # attribute that points back to the *original* function. That is great for decorators,
    # but since clone_function aims to build a true *copy*, it deletes __wrapped__.
    assert not hasattr(clone_ft, "__wrapped__"), "clone must not keep __wrapped__"
