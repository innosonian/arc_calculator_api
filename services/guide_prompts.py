import json
import os
from typing import Any

from services.http.schemas import comp_vent_targets

_PROMPT_BOOK_DIR = os.path.join(os.path.dirname(__file__), "..", "resources", "prompt_books")

# 완벽(만점) 지표는 교정 문구를 내지 않는다. 코칭 슬롯을 채우려고 잘 수행한 지표에
# 엉뚱한 교정 문구를 붙이는 것을 막는다(정렬 오름차순이므로 이 값에 도달하면 이후도 모두 만점).
_PERFECT_SCORE = 100

_TABLE_OF_PROMPT = {
    "language_guideline": {
        "ERC 2020": {
            "default": "prompt_book_2020erc_eng_uk.json",
            "british": "prompt_book_2020erc_eng_uk.json",
            "korean": "prompt_book_2020erc_korean.json",
            "usa": "prompt_book_2020erc_eng_us.json",
        },
        "ARC 2020": {
            "default": "prompt_book_2020arc_eng_us.json",
            "british": "prompt_book_2020arc_eng_uk.json",
            "korean": "prompt_book_2020arc_korean.json",
            "usa": "prompt_book_2020arc_eng_us.json",
        },
        "AHA 2020": {
            "default": "prompt_book_2020arc_eng_uk.json",
            "british": "prompt_book_2020arc_eng_uk.json",
            "korean": "prompt_book_2020arc_korean.json",
            "usa": "prompt_book_2020arc_eng_us.json",
        },
        "2020 ERC": {
            "default": "prompt_book_2020erc_eng_uk.json",
            "british": "prompt_book_2020erc_eng_uk.json",
            "korean": "prompt_book_2020erc_korean.json",
            "usa": "prompt_book_2020erc_eng_us.json",
        },
        "2020 ARC": {
            "default": "prompt_book_2020arc_eng_us.json",
            "british": "prompt_book_2020arc_eng_uk.json",
            "korean": "prompt_book_2020arc_korean.json",
            "usa": "prompt_book_2020arc_eng_us.json",
        },
        "2020 AHA": {
            "default": "prompt_book_2020arc_eng_uk.json",
            "british": "prompt_book_2020arc_eng_uk.json",
            "korean": "prompt_book_2020arc_korean.json",
            "usa": "prompt_book_2020arc_eng_us.json",
        },
        "pre2020": {
            "default": "prompt_book_eng_uk.json",
            "british": "prompt_book_eng_uk.json",
            "usa": "prompt_book_eng_us.json",
            "korean": "prompt_book_korean.json",
        },
        "2015 version": {
            "default": "prompt_book_eng_uk.json",
            "british": "prompt_book_eng_uk.json",
            "usa": "prompt_book_eng_us.json",
            "korean": "prompt_book_korean.json",
        },
    }
}


def load_prompt_book(guideline: str | None, regional_option: str | None) -> dict | None:
    guideline_key = _resolve_prompt_source(guideline)
    region_key = _normalize_region(regional_option)
    return _load_prompt_book_file(guideline_key, region_key)


def _load_prompt_book_file(guideline_key: str, region_key: str) -> dict | None:
    table = _TABLE_OF_PROMPT["language_guideline"]
    guideline_table = table.get(guideline_key) or table.get("2015 version")
    if not guideline_table:
        return None
    filename = guideline_table.get(region_key) or guideline_table.get("default")
    if not filename:
        return None
    path = os.path.join(_PROMPT_BOOK_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _resolve_prompt_source(guideline: str | None) -> str:
    if not guideline:
        return "2015 version"
    value = guideline.strip().upper()
    if "2025" in value:
        if "ARC" in value:
            return "ARC 2020"
        return "2015 version"
    if "2020" in value:
        if "ERC" in value:
            return "ERC 2020"
        if "ARC" in value:
            return "ARC 2020"
        if "AHA" in value:
            return "AHA 2020"
        return "2020 AHA"
    if "2015" in value or "PRE2020" in value:
        return "2015 version"
    return "2015 version"


def _normalize_region(regional_option: str | None) -> str:
    if not regional_option:
        return "british"
    return regional_option.strip().lower()


def build_guide_prompts(
    calculation_result: dict,
    condition: dict | None,
    usage: dict | None,
) -> list[str]:
    total_score = calculation_result.get("cpr_total_scores") or {}
    overall = total_score.get("overall")
    if overall in (None, 0):
        return []

    regional_option = None
    if isinstance(usage, dict):
        regional_option = usage.get("Regional_Option")

    prompts_text = load_prompt_book((condition or {}).get("guideline"), regional_option)
    if not prompts_text:
        return []

    comp_target, vent_target = comp_vent_targets(condition)

    result_by_cycle = _build_result_by_cycle(total_score)
    result_by_criteria = _build_result_by_criteria(calculation_result.get("cpr_metrics") or {})

    generator = _select_generator(
        condition, prompts_text, result_by_cycle, result_by_criteria, total_score, comp_target, vent_target
    )
    prompts = generator.run()
    return ["• " + prompt for prompt in prompts]


def _select_generator(
    condition: dict | None,
    prompts_text: dict,
    result_by_cycle: dict,
    result_by_criteria: dict,
    total_score: dict,
    comp_target: int,
    vent_target: int,
):
    training_type = (condition or {}).get("training_type") or "cpr"
    target = (condition or {}).get("target") or "adult"

    generator_cls, section = _pick_generator_and_section(prompts_text, training_type, target)
    return generator_cls(section, result_by_cycle, result_by_criteria, total_score, comp_target, vent_target)


def _pick_generator_and_section(prompts_text: dict, training_type: str, target: str):
    if training_type == "ventilation_only":
        if target == "infant":
            return _GuidePromptBabyVent, _pick_section(prompts_text, ["Baby Ventilation only"])
        if target == "child":
            return _GuidePromptChildVent, _pick_section(prompts_text, ["Child Ventilation only", "Child ventilation only"])
        return _GuidePromptAdultVent, _pick_section(prompts_text, ["Ventilation only"])

    if training_type == "compression_only":
        if target == "infant":
            return _GuidePromptBabyComp, _pick_section(prompts_text, ["Baby Chest compression only"])
        if target == "child":
            return _GuidePromptChildComp, _pick_section(prompts_text, ["Child Chest compression only"])
        return _GuidePromptAdultComp, _pick_section(prompts_text, ["Chest compression only"])

    if target == "infant":
        return _GuidePromptBabyCPR, _pick_section(prompts_text, ["Baby CPR Training"])
    if target == "child":
        return _GuidePromptChildCPR, _pick_section(prompts_text, ["Child CPR Training"])
    return _GuidePromptAdultCPR, _pick_section(prompts_text, ["CPR Training"])


def _pick_section(prompts_text: dict, keys: list[str]) -> dict:
    for key in keys:
        section = prompts_text.get(key)
        if isinstance(section, dict):
            return section
    return {}


def _build_result_by_cycle(total_score: dict) -> dict:
    def score(key: str) -> float:
        value = total_score.get(key)
        return 0 if value is None else value

    return {
        "CompressionDepth": {"Overall": score("score_comp_depth")},
        "Recoil": {"Overall": score("score_recoil")},
        "CompressionRate": {"Overall": score("score_comp_rate")},
        "ScoreOfCCF": {"Overall": score("score_ccf")},
        "HandPosition": {"Overall": score("score_hand_position")},
        "CompressionNo": {"Overall": score("score_comp_no")},
        # 속도 코칭은 '측정 가능' 신호를 쓰고 None(측정 불가)을 보존한다(0으로 강등하지 않음)
        # — 아티팩트로 속도 코칭이 잘못 뽑히는 것을 막기 위함. sort 단계에서 None은 지표에서 제외한다.
        "VentilationRate": {"Overall": total_score.get("score_vent_rate_measured")},
        "VentilationVolume": {"Overall": score("score_vent_vol")},
        "VentilationSpeed": {"Overall": score("score_vent_speed")},
        "VentilationCount": {"Overall": score("score_vent_count")},
    }


def _build_result_by_criteria(metrics: dict) -> dict:
    result: dict[str, Any] = {}
    for key in (
        "CompressionDepth",
        "CompressionRate",
        "Recoil",
        "HandPosition",
        "CompressionNo",
        "VentilationVolume",
        "VentilationRate",
        "VentilationSpeed",
    ):
        if isinstance(metrics.get(key), dict):
            result[key] = metrics[key]

    score_of_ccf = metrics.get("ScoreOfCCF")
    if score_of_ccf is not None:
        result["ScoreOfCCF"] = score_of_ccf
    return result


class _GuidePromptBase:
    def __init__(
        self,
        prompts_text: dict,
        result_by_cycle: dict,
        result_by_criteria: dict,
        total_score: dict,
        comp_target: int = 30,
        vent_target: int = 2,
    ):
        self.prompts_text = prompts_text or {}
        self.result_by_cycle = result_by_cycle
        self.result_by_crit = result_by_criteria
        self.total_score = total_score.get("overall") or 0
        # cpr_cycle_type(=CompVentRatio)에서 유도한 사이클당 압박/환기 목표.
        self.comp_target = comp_target
        self.vent_target = vent_target
        self.score_items: dict[str, float] = {}
        self.sorted_for_1st: list[tuple[str, float]] = []
        self.sorted_for_extra: list[tuple[str, float]] = []
        self.guide_prompt_list: list[str] = []

    @staticmethod
    def _fill_count(text: str | None, count: int) -> str | None:
        # 프롬프트북의 "%d" 자리에 목표 숫자를 채운다. str.replace를 써서 다른 '%'가 섞여도
        # 예외가 나지 않고 리터럴 "%d"가 노출되지 않는다. "%d"가 없으면 원문 그대로 반환된다.
        if isinstance(text, str):
            return text.replace("%d", str(count))
        return text

    def _put_score(self, key: str, value: float | None) -> None:
        # 지표 점수를 정렬 대상에 넣되, None(측정 불가, 예: 속도를 잴 수 없는 환기 세션)은
        # 지표에서 제외한다(정렬 시 None 비교 오류 방지 + 측정 불가 지표는 코칭하지 않음).
        if value is None:
            self.score_items.pop(key, None)
        else:
            self.score_items[key] = value

    def run(self) -> list[str]:
        self.init_prompt()
        self.create_prompt()
        return self.guide_prompt_list

    def init_prompt(self) -> None:
        raise NotImplementedError

    def create_prompt(self) -> None:
        raise NotImplementedError

    def is_score_zero(self) -> bool:
        return self.total_score == 0

    def is_score_perfect(self) -> bool:
        return self.total_score == 100

    def is_score_poor(self) -> bool:
        return self.total_score < 60

    def get_1st_prompt(self) -> str | None:
        prompt_list = self.prompts_text.get("1st prompt") or []
        if self.total_score == 100 and len(prompt_list) > 0:
            return prompt_list[0]
        if self.total_score < 60 and len(prompt_list) > 3:
            return prompt_list[3]
        if self.total_score < 80 and len(prompt_list) > 2 and isinstance(prompt_list[2], dict):
            key, value = self.sorted_for_1st[-1]
            item = prompt_list[2].get(key)
            if isinstance(item, list) and len(item) >= 2:
                return f"{item[0]}{value}{item[1]}"
        if self.total_score < 100 and len(prompt_list) > 1 and isinstance(prompt_list[1], dict):
            key, _value = self.sorted_for_1st[-1]
            return prompt_list[1].get(key)
        return None

    @staticmethod
    def _sorted_critic(low: float, mid: float, high: float) -> str:
        sorted_d = {0: low, 1: mid, 2: high}
        most_frequent = sorted(sorted_d.items(), key=lambda x: x[-1])[-1][0]
        items = {0: "under", 1: "wrong", 2: "over"}
        return items[most_frequent]

    def create_prompt_list(self) -> None:
        if self.is_score_zero():
            return
        first_prompt = self.get_1st_prompt()
        if first_prompt:
            self.guide_prompt_list.append(first_prompt)
        if self.is_score_perfect():
            return
        # 낮은 점수(가장 취약한 지표)부터 순회하며 실제 코칭만 채운다. 억제(None)된 지표는
        # 건너뛰고 다음 최저로 넘어가므로, 특정 지표가 억제되어도 코칭 슬롯이 비지 않는다.
        needed = 2 if self.is_score_poor() else 1
        self._append_extra_prompts(needed)

    def _append_extra_prompts(self, needed: int) -> None:
        filled = 0
        for num in range(len(self.sorted_for_extra)):
            if filled >= needed:
                break
            # 완벽(만점) 지표는 교정하지 않는다. 오름차순 정렬이라 여기 도달하면 이후도 모두 만점.
            if self.sorted_for_extra[num][1] >= _PERFECT_SCORE:
                break
            extra = self.get_extra_prompt(num)
            if extra:
                self.guide_prompt_list.append(extra)
                filled += 1

    def get_extra_prompt(self, _num: int) -> str | None:
        return None


class _GuidePromptAdultVent(_GuidePromptBase):
    def init_prompt(self) -> None:
        self.score_items = {"VentilationVolume": 0, "VentilationRate": 0}

    def create_prompt(self) -> None:
        self.sort_vent_score_items()
        self.create_prompt_list()

    def sort_vent_score_items(self) -> None:
        self.score_items["VentilationVolume"] = self.result_by_cycle["VentilationVolume"]["Overall"]
        self._put_score("VentilationRate", self.result_by_cycle["VentilationRate"]["Overall"])
        self.sorted_for_1st = sorted(self.score_items.items(), key=lambda x: x[1])
        self.sorted_for_extra = sorted(self.score_items.items(), key=lambda x: x[1])

    def get_extra_prompt(self, num: int) -> str | None:
        prompts = (self.prompts_text.get("2nd 3rd prompt") or {})
        key = self.sorted_for_extra[num][0]
        if key == "VentilationRate":
            return prompts.get("VentilationRate")
        if key == "VentilationVolume":
            return (prompts.get("VentilationVolume") or {}).get(self.get_critic_vent_vol())
        return None

    def get_critic_vent_vol(self) -> str:
        low = (self.result_by_crit.get("VentilationVolume") or {}).get("%_TooLittle", 0)
        mid = (self.result_by_crit.get("VentilationVolume") or {}).get("%_Good", 0)
        high = (self.result_by_crit.get("VentilationVolume") or {}).get("%_TooMuch", 0)
        return self._sorted_critic(low, mid, high)


class _GuidePromptAdultComp(_GuidePromptBase):
    def init_prompt(self) -> None:
        self.score_items = {
            "CompressionDepth": 0,
            "CompressionRate": 0,
            "HandPosition": 0,
            "ScoreOfCCF": 0,
            "Recoil": 0,
        }

    def create_prompt(self) -> None:
        self.sort_comp_score_items()
        self.create_prompt_list()

    def sort_comp_score_items(self) -> None:
        self.score_items["CompressionDepth"] = self.result_by_cycle["CompressionDepth"]["Overall"]
        self.score_items["Recoil"] = self.result_by_cycle["Recoil"]["Overall"]
        self.score_items["CompressionRate"] = self.result_by_cycle["CompressionRate"]["Overall"]
        self.score_items["ScoreOfCCF"] = self.result_by_cycle["ScoreOfCCF"]["Overall"]
        self.sorted_for_1st = sorted(self.score_items.items(), key=lambda x: x[1])
        self.score_items["HandPosition"] = self.result_by_cycle["HandPosition"]["Overall"]
        self.sorted_for_extra = sorted(self.score_items.items(), key=lambda x: x[1])

    def get_extra_prompt(self, num: int) -> str | None:
        prompts = (self.prompts_text.get("2nd 3rd prompt") or {})
        key = self.sorted_for_extra[num][0]
        if key == "CompressionDepth":
            return (prompts.get("CompressionDepth") or {}).get(self.get_critic_comp_depth())
        if key == "CompressionRate":
            return (prompts.get("CompressionRate") or {}).get(self.get_critic_comp_rate())
        if key == "HandPosition":
            return prompts.get("HandPosition")
        if key == "ScoreOfCCF":
            return prompts.get("ScoreOfCCF")
        if key == "Recoil":
            return prompts.get("Recoil")
        return None

    def get_critic_comp_depth(self) -> str:
        low = (self.result_by_crit.get("CompressionDepth") or {}).get("%_TooShallow", 0)
        mid = (self.result_by_crit.get("CompressionDepth") or {}).get("%_Good", 0)
        high = (self.result_by_crit.get("CompressionDepth") or {}).get("%_TooDeep", 0)
        return self._sorted_critic(low, mid, high)

    def get_critic_comp_rate(self) -> str:
        low = (self.result_by_crit.get("CompressionRate") or {}).get("%_TooSlow", 0)
        mid = (self.result_by_crit.get("CompressionRate") or {}).get("%_Good", 0)
        high = (self.result_by_crit.get("CompressionRate") or {}).get("%_TooFast", 0)
        return self._sorted_critic(low, mid, high)


class _GuidePromptAdultCPR(_GuidePromptAdultComp, _GuidePromptAdultVent):
    def init_prompt(self) -> None:
        self.score_items = {
            "CompressionDepth": 0,
            "Recoil": 0,
            "CompressionRate": 0,
            "ScoreOfCCF": 0,
            "VentilationVolume": 0,
        }

    def create_prompt(self) -> None:
        self.sort_cpr_score_items()
        self.create_prompt_list()

    def sort_cpr_score_items(self) -> None:
        self.score_items["CompressionDepth"] = self.result_by_cycle["CompressionDepth"]["Overall"]
        self.score_items["Recoil"] = self.result_by_cycle["Recoil"]["Overall"]
        self.score_items["CompressionRate"] = self.result_by_cycle["CompressionRate"]["Overall"]
        self.score_items["ScoreOfCCF"] = self.result_by_cycle["ScoreOfCCF"]["Overall"]
        self.score_items["VentilationVolume"] = self.result_by_cycle["VentilationVolume"]["Overall"]
        self.sorted_for_1st = sorted(self.score_items.items(), key=lambda x: x[1])
        self.score_items["HandPosition"] = self.result_by_cycle["HandPosition"]["Overall"]
        self.score_items["CompressionNo"] = self.result_by_cycle["CompressionNo"]["Overall"]
        # 속도는 측정 가능할 때만 지표에 포함(측정 불가면 None → 제외). 횟수는 항상 수치.
        self._put_score("VentilationRate", self.result_by_cycle["VentilationRate"]["Overall"])
        self.score_items["VentilationCount"] = self.result_by_cycle["VentilationCount"]["Overall"]
        self.sorted_for_extra = sorted(self.score_items.items(), key=lambda x: x[1])

    def get_extra_prompt(self, num: int) -> str | None:
        prompts = (self.prompts_text.get("2nd 3rd prompt") or {})
        key = self.sorted_for_extra[num][0]
        if key == "CompressionDepth":
            return (prompts.get("CompressionDepth") or {}).get(self.get_critic_comp_depth())
        if key == "CompressionRate":
            return (prompts.get("CompressionRate") or {}).get(self.get_critic_comp_rate())
        if key == "HandPosition":
            return prompts.get("HandPosition")
        if key == "ScoreOfCCF":
            return prompts.get("ScoreOfCCF")
        if key == "Recoil":
            return prompts.get("Recoil")
        if key == "CompressionNo":
            # 압박 횟수 문구의 숫자는 CompVentRatio에서 유도한 목표로 채운다(30/15 등).
            return self._fill_count(prompts.get("CompressionNo"), self.comp_target)
        if key == "VentilationRate":
            # 속도 코칭. 측정 가능한 사이클이 있어 지표로 편입됐을 때만 여기 도달한다(measured 신호).
            return prompts.get("VentilationRate")
        if key == "VentilationCount":
            # 횟수 코칭. 숫자는 CompVentRatio 환기값(vent_target=2)에서 유도한다.
            # 만점(정확히 2회)이면 _append_extra_prompts의 완벽지표 가드가 걸러 출력되지 않는다.
            return self._fill_count(prompts.get("VentilationCount"), self.vent_target)
        if key == "VentilationVolume":
            return (prompts.get("VentilationVolume") or {}).get(self.get_critic_vent_vol())
        return None


class _GuidePromptBabyVent(_GuidePromptAdultVent):
    def sort_vent_score_items(self) -> None:
        self.score_items["VentilationVolume"] = self.result_by_cycle["VentilationVolume"]["Overall"]
        self._put_score("VentilationRate", self.result_by_cycle["VentilationRate"]["Overall"])
        self.sorted_for_1st = sorted(self.score_items.items(), key=lambda x: x[1])
        self.score_items["VentilationSpeed"] = self.result_by_cycle["VentilationSpeed"]["Overall"]
        self.sorted_for_extra = sorted(self.score_items.items(), key=lambda x: x[1])

    def get_extra_prompt(self, num: int) -> str | None:
        prompts = (self.prompts_text.get("2nd 3rd prompt") or {})
        key = self.sorted_for_extra[num][0]
        if key == "VentilationRate":
            return prompts.get("VentilationRate")
        if key == "VentilationVolume":
            return (prompts.get("VentilationVolume") or {}).get(self.get_critic_vent_vol())
        if key == "VentilationSpeed":
            return prompts.get("VentilationSpeed")
        return None


class _GuidePromptBabyComp(_GuidePromptAdultComp):
    pass


class _GuidePromptBabyCPR(_GuidePromptAdultCPR):
    def sort_cpr_score_items(self) -> None:
        super().sort_cpr_score_items()
        self.score_items["VentilationSpeed"] = self.result_by_cycle["VentilationSpeed"]["Overall"]
        self.sorted_for_extra = sorted(self.score_items.items(), key=lambda x: x[1])

    def get_extra_prompt(self, num: int) -> str | None:
        prompts = (self.prompts_text.get("2nd 3rd prompt") or {})
        key = self.sorted_for_extra[num][0]
        if key == "VentilationSpeed":
            return prompts.get("VentilationSpeed")
        return super().get_extra_prompt(num)


class _GuidePromptChildVent(_GuidePromptAdultVent):
    pass


class _GuidePromptChildComp(_GuidePromptAdultComp):
    pass


class _GuidePromptChildCPR(_GuidePromptAdultCPR):
    pass
