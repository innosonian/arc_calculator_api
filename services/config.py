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
        self.divider = PartDivider(self.calculation_config)

    def is_2rescuers(self) -> bool:
        return bool(self.condition.get("is_2rescuers"))
