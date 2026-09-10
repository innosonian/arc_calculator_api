from abc import ABCMeta, abstractmethod

from services.http.schemas import ConditionType

AED_TIME_PART_FIRST = [55, 65]
AED_TIME_PART_OTHERS = [5, 10]


class Border(metaclass=ABCMeta):
    @abstractmethod
    def get_border(self, key) -> dict:
        pass

    @abstractmethod
    def get_aed_guide_sec(self, part_num: int) -> int:
        pass


class BaseBorder(Border):
    BORDER = {}

    def __init__(self, guideline: str = "AHA2020"):
        self.guideline = guideline
        self.border = self.BORDER[guideline]

    def get_border(self, key) -> dict:
        return self.border.get(key)

    def get_aed_guide_sec(self, part_num: int) -> int:
        return self.border["aed_time"][0] if part_num == 1 else self.border["aed_time_2"][0]


class AdultBorder(BaseBorder):
    BORDER = {
        "AHA2020": {
            "comp_depth": [40, 50, 60, 70],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [25, 29, 31, 35],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "down": [0, "down"],
                "left": [20, "side"],
                "right": [20, "side"],
            },
            "vent_vol": [300, 400, 700, 800],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [7, 8, 12, 13],
            "vent_speed": [200, 300, 800, 900],
            "vent_rate_1p": [2, 4, 12, 14],  # 1인 구조자 일 때
            "ccf_1p": [40, 70],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
        },
        "ARC2020": {
            "comp_depth": [40, 50, 60, 70],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [300, 400, 600, 700],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [7, 8, 12, 13],
            "vent_speed": [200, 300, 800, 900],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [40, 70],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
        },
        "ERC2020": {
            "comp_depth": [40, 50, 60, 70],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [300, 400, 700, 800],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [7, 8, 12, 13],
            "vent_speed": [200, 300, 800, 900],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [40, 70],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
        },
        "STD2015": {
            "comp_depth": [40, 50, 60, 70],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [300, 400, 700, 800],
            "vent_cnt": {
                "0": [0, "too_few"],
                "1": [50, "too_few"],
                "2": [100, "good"],
                "3": [50, "too_many"],
                "4": [0, "too_many"],
            },
            "vent_only_rate": [7, 8, 12, 13],
            "vent_speed": [200, 300, 800, 900],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [40, 70],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
        },
        "ARC2025": {
            "comp_depth": [40, 50, 60, 70],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [300, 400, 600, 700],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [7, 8, 12, 13],
            "vent_speed": [200, 300, 800, 900],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [40, 70],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
        },
    }


class ChildBorder(BaseBorder):
    BORDER = {
        "AHA2020": {
            "comp_depth": [40, 50, 60, 70],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [25, 29, 31, 35],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "down": [0, "down"],
                "left": [20, "side"],
                "right": [20, "side"],
            },
            "vent_vol": [300, 400, 700, 800],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [15, 20, 30, 40],
            "vent_speed": [200, 300, 1200, 5000],
            "vent_rate_1p": [2, 4, 12, 14],  # 1인 구조자 일 때
            "ccf_1p": [40, 70],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
            "init_rescue_cnt": [3, 5, 5, 7],
            "init_rescue_rate": [15, 20, 40, 60],
        },
        "ARC2020": {
            "comp_depth": [40, 50, 60, 70],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [300, 400, 600, 700],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [15, 20, 30, 40],
            "vent_speed": [200, 300, 1200, 5000],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [40, 70],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
            "init_rescue_cnt": [3, 5, 5, 7],
            "init_rescue_rate": [15, 20, 40, 60],
        },
        "ERC2020": {
            "comp_depth": [40, 50, 60, 70],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [100, 200, 500, 700],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [12, 15, 20, 24],
            "vent_speed": [200, 300, 1200, 5000],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [40, 70],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
            "init_rescue_cnt": [3, 5, 5, 7],
            "init_rescue_rate": [15, 20, 40, 60],
        },
        "STD2015": {
            "comp_depth": [40, 50, 60, 70],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [300, 400, 700, 800],
            "vent_cnt": {
                "0": [0, "too_few"],
                "1": [50, "too_few"],
                "2": [100, "good"],
                "3": [50, "too_many"],
                "4": [0, "too_many"],
            },
            "vent_only_rate": [7, 12, 20, 25],
            "vent_speed": [200, 300, 800, 900],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [40, 70],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
            "init_rescue_cnt": [3, 5, 5, 7],
            "init_rescue_rate": [15, 20, 40, 60],
        },
        "ARC2025": {
            "comp_depth": [40, 50, 60, 70],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [300, 400, 600, 700],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [15, 20, 30, 40],
            "vent_speed": [200, 300, 1200, 5000],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [40, 70],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
            "init_rescue_cnt": [3, 5, 5, 7],
            "init_rescue_rate": [15, 20, 40, 60],
        },
    }


class InfantBorder(BaseBorder):
    BORDER = {
        "AHA2020": {
            "comp_depth": [25, 33, 40, 48],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [25, 29, 31, 35],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "down": [0, "down"],
                "left": [20, "side"],
                "right": [20, "side"],
            },
            "vent_vol": [10, 20, 40, 60],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [15, 20, 30, 40],
            "vent_speed": [200, 300, 1200, 5000],
            "vent_rate_1p": [2, 4, 12, 14],  # 1인 구조자 일 때
            "ccf_1p": [10, 55],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
            "init_rescue_cnt": [3, 5, 5, 7],
            "init_rescue_rate": [15, 20, 40, 60],
            "vent_vol_compensation": 1,
        },
        "ARC2020": {
            "comp_depth": [25, 33, 40, 48],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [10, 20, 40, 60],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [15, 20, 30, 40],
            "vent_speed": [200, 300, 1200, 5000],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [10, 55],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
            "init_rescue_cnt": [3, 5, 5, 7],
            "init_rescue_rate": [15, 20, 40, 60],
            "vent_vol_compensation": 1,
        },
        "ERC2020": {
            "comp_depth": [25, 33, 40, 48],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [15, 30, 50, 70],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [15, 20, 30, 40],
            "vent_speed": [200, 300, 1200, 5000],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [10, 55],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
            "init_rescue_cnt": [3, 5, 5, 7],
            "init_rescue_rate": [15, 20, 40, 60],
            "vent_vol_compensation": 1,
        },
        "STD2015": {
            "comp_depth": [25, 33, 40, 48],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [10, 20, 40, 60],
            "vent_cnt": {
                "0": [0, "too_few"],
                "1": [50, "too_few"],
                "2": [100, "good"],
                "3": [50, "too_many"],
                "4": [0, "too_many"],
            },
            "vent_only_rate": [7, 12, 20, 25],
            "vent_speed": [200, 300, 1000, 1500],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [10, 55],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
            "init_rescue_cnt": [3, 5, 5, 7],
            "init_rescue_rate": [15, 20, 40, 60],
            "vent_vol_compensation": 1,
        },
        "ARC2025": {
            "comp_depth": [25, 33, 40, 48],
            "comp_rate": [90, 100, 120, 130],
            "comp_cnt_302": [27, 29, 31, 33],
            "comp_cnt_152": [13, 15, 15, 17],
            "recoil": [5, 10],
            "hand_position": {
                "center": [100, "good"],
                "left": [20, "side"],
                "right": [20, "side"],
                "down": [0, "down"],
            },
            "vent_vol": [10, 20, 40, 60],
            "vent_cnt": {
                "0": [0, "too_few"],
                "2": [100, "good"],
                "1": [50, "too_few"],
                "3": [50, "too_many"],
            },
            "vent_only_rate": [15, 20, 30, 40],
            "vent_speed": [200, 300, 1200, 5000],
            "vent_rate_1p": [2, 4, 12, 14],
            "ccf_1p": [10, 55],
            "ccf_2p": [50, 80],
            "ccf_comp": [70, 100],
            "aed_time": AED_TIME_PART_FIRST,
            "aed_time_2": AED_TIME_PART_OTHERS,
            "init_rescue_cnt": [3, 5, 5, 7],
            "init_rescue_rate": [15, 20, 40, 60],
            "vent_vol_compensation": 1,
        },
    }


class BorderFactory:
    @staticmethod
    def create_border(condition: ConditionType) -> BaseBorder:
        if condition["target"].upper() == "ADULT":
            return AdultBorder(condition["guideline"])
        elif condition["target"].upper() == "CHILD":
            return ChildBorder(condition["guideline"])
        elif condition["target"].upper() == "INFANT":
            return InfantBorder(condition["guideline"])


def trapezium_get_point(border: BaseBorder, border_key: str, value: int) -> dict:
    border_range = border.get_border(border_key)

    if border_range[1] <= value <= border_range[2]:
        return {
            "grade": 100,
            "criterion": "good",
        }
    elif border_range[0] < value < border_range[1]:
        return {
            "grade": int(((value - border_range[0]) / (border_range[1] - border_range[0])) * 100),
            "criterion": "low",
        }
    elif border_range[2] < value < border_range[3]:
        return {
            "grade": int(((border_range[3] - value) / (border_range[3] - border_range[2])) * 100),
            "criterion": "high",
        }
    elif value <= border_range[0]:
        return {
            "grade": 0,
            "criterion": "low",
        }
    elif value >= border_range[3]:
        return {
            "grade": 0,
            "criterion": "high",
        }

    return {
        "grade": 0,
        "criterion": "low",
    }


def fall_linear_get_point(border: BaseBorder, border_key: str, value: int) -> dict:
    border_range = border.get_border(border_key)

    if value <= border_range[0]:
        return {
            "grade": 100,
            "criterion": "good",
        }
    elif border_range[0] < value < border_range[1]:
        return {
            "grade": int(((border_range[1] - value) / (border_range[1] - border_range[0])) * 100),
            "criterion": "bad",
        }

    return {
        "grade": 0,
        "criterion": "bad",
    }


def dart_get_point(border: BaseBorder, border_key: str, value: str) -> dict:
    border_range = border.get_border(border_key)

    if score := border_range.get(value):
        return {"grade": score[0], "criterion": score[1]}

    if border_key == "vent_cnt":
        return {"grade": 0, "criterion": "too_many"}

    return {"grade": 0, "criterion": "bad"}


def rise_linear_get_point(border: BaseBorder, border_key: str, value: int):
    border_range = border.get_border(border_key)

    if value < border_range[0]:
        return {"grade": 0, "criterion": "bad"}
    elif border_range[0] <= value < border_range[1]:
        return {
            "grade": int((value - border_range[0]) / (border_range[1] - border_range[0]) * 100),
            "criterion": "bad",
        }

    return {"grade": 100, "criterion": "good"}
