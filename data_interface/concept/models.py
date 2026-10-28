"""概念板块领域模型与常量。"""

from __future__ import annotations

from dataclasses import dataclass

SOURCE_THS = "akshare.ths_index"
SOURCE_THS_SCRAPE = "ths.scrape"
SOURCE_EM = "akshare.em_board"
SOURCE_TUSHARE_CONCEPT = "tushare.concept"

BOARD_TYPE_CONCEPT = "concept"


@dataclass(frozen=True)
class Board:
    """规范板块（``board_code`` 当前 = 同花顺 6 位代码）。"""

    board_code: str
    name: str
    source: str
    source_code: str | None = None
    board_type: str = BOARD_TYPE_CONCEPT

    def as_dict(self) -> dict[str, str | None]:
        return {
            "board_code": self.board_code,
            "name": self.name,
            "board_type": self.board_type,
            "source": self.source,
            "source_code": self.source_code or self.board_code,
        }
