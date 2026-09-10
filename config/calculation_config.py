from abc import ABCMeta, abstractmethod

from services.http.schemas import ConditionType


class CalculationConfig(metaclass=ABCMeta):
    @abstractmethod
    def get_hand_position_threshold_level(self, level: str = "normal") -> int:
        pass

    @abstractmethod
    def get_vent_vol_compensation(self) -> int:
        pass


class BaseCalculationConfig(CalculationConfig):
    GUIDELINES = {}

    def __init__(self, guideline: str, condition: ConditionType):
        self.guideline = self.GUIDELINES[guideline]
        self.condition = condition

    def get_hand_position_threshold_level(self, level: str = "normal") -> int:
        return self.guideline["hand_position_threshold_configuration"][level]

    def get_vent_vol_compensation(self) -> int:
        return self.guideline["vent_vol_compensation"]

    def get_manikin_type(self) -> str:
        pass

    def get_person_type(self) -> str:
        return "1p"

    def is_infant(self) -> bool:
        return self.condition.get("target", "") == "infant"

    def is_child(self) -> bool:
        return self.condition.get("target", "") == "child"

    def is_cpr(self) -> bool:
        return self.condition.get("training_type", "") == "cpr"

    def is_cco(self) -> bool:
        return self.condition.get("training_type", "") == "compression_only"

    def is_vent_only(self) -> bool:
        return self.condition.get("training_type", "") == "ventilation_only"

    def is_contain_comp(self) -> bool:
        return self.is_cpr() or self.is_cco()

    def need_rescue_vent(self) -> bool:
        return (self.is_infant() or self.is_child()) and self.condition["guideline"] == "ERC2020" and self.is_cpr()


class AdultCalculationConfig(BaseCalculationConfig):
    STANDARD = {
        "compression_threshold_interval_length": 200,
        "compression_threshold_peak": 90,
        "compression_threshold_depth": 30,
        "hand_position_threshold_configuration": {
            "strict": 10,
            "normal": 5,
            "generous": 3,
        },
        "vent_vol_compensation": 10,
    }

    GUIDELINES = {
        "AHA2020": STANDARD,
        "ARC2020": STANDARD,
        "ARC2025": STANDARD,
        "ERC2020": STANDARD,
        "STD2015": STANDARD,
    }

    def get_manikin_type(self) -> str:
        return "adult"


class ChildCalculationConfig(BaseCalculationConfig):
    STANDARD = {
        "compression_threshold_interval_length": 200,
        "compression_threshold_peak": 90,
        "compression_threshold_depth": 30,
        "hand_position_threshold_configuration": {
            "strict": 10,
            "normal": 5,
            "generous": 3,
        },
        "vent_vol_compensation": 10,
    }

    GUIDELINES = {
        "AHA2020": STANDARD,
        "ARC2020": STANDARD,
        "ARC2025": STANDARD,
        "ERC2020": STANDARD,
        "STD2015": STANDARD,
    }

    def get_manikin_type(self) -> str:
        return "child"


class InfantCalculationConfig(BaseCalculationConfig):
    STANDARD = {
        "compression_threshold_interval_length": 200,
        "compression_threshold_peak": 60,
        "compression_threshold_depth": 17,
        "hand_position_threshold_configuration": {
            "strict": 10,
            "normal": 5,
            "generous": 3,
        },
        "vent_vol_compensation": 1,
    }

    GUIDELINES = {
        "AHA2020": STANDARD,
        "ARC2020": STANDARD,
        "ARC2025": STANDARD,
        "ERC2020": STANDARD,
        "STD2015": STANDARD,
    }

    def get_manikin_type(self) -> str:
        return "infant"


class CalculationConfigFactory:
    @staticmethod
    def create_calculation_config(condition: ConditionType) -> BaseCalculationConfig:
        if condition["target"] == "adult":
            return AdultCalculationConfig(condition["guideline"], condition)
        elif condition["target"] == "child":
            return ChildCalculationConfig(condition["guideline"], condition)
        elif condition["target"] == "infant":
            return InfantCalculationConfig(condition["guideline"], condition)
