import struct

import pytest


HEADER = bytes.fromhex("feffffffffffff7f")


@pytest.fixture
def dat_file():
    def make(path, *rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(HEADER + b"".join(struct.pack("<16I", *row) for row in rows))
        return path

    return make


@pytest.fixture
def row():
    return (1790046000, 7100, 7350, 7000, 7250, 0, 1234, 0,
            5, 1, 123456789, 23, 77, 7000, 0, 0)
