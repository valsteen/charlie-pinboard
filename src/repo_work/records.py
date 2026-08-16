from typing import dataclass_transform

import msgspec


@dataclass_transform(frozen_default=True)
class Record(msgspec.Struct, frozen=True):
    pass


@dataclass_transform(frozen_default=True)
class JsonRecord(Record, forbid_unknown_fields=True, omit_defaults=True):
    pass
