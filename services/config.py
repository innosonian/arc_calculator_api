# 이식 출처: 원본 hstm_v2 services/config.py (22줄).
# 원본 대비 차이: PartDivider 생성이 무인자 — 원본의 calculation_config 파라미터는 저장만 되고
# 읽히지 않는 dead 파라미터라 미이식(스펙 §4.5). 나머지 동작 동일.
from config.borders import BorderFactory, BaseBorder
from config.calculation_config import CalculationConfigFactory, BaseCalculationConfig
from services.http.schemas import ConditionType
from transformers.part_divider import PartDivider


class Config:
    calculation_config: BaseCalculationConfig
    border: BaseBorder
    divider: PartDivider

    def __init__(self, condition: ConditionType) -> None:
        self.condition = condition
        self.__set_config()

    def __set_config(self, *args, **kwargs) -> None:
        self.calculation_config = CalculationConfigFactory.create_calculation_config(self.condition)
        self.border = BorderFactory.create_border(self.condition)
        self.divider = PartDivider()

    def is_2rescuers(self) -> bool:
        return bool(self.condition.get("is_2rescuers"))
