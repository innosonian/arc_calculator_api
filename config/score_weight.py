class ScoreWeight:
    COMP_DEPTH: float
    COMP_RATE: float
    RECOIL: float
    HAND_POSITION: float
    COMP_COUNT: float
    CCF: float
    VENT_VOL: float
    VENT_COUNT: float
    VENT_SPEED: float
    VENT_RATE: float
    RESCUE_VENT: float
    RESCUE_VENT_VOLUME: float
    RESCUE_VENT_COUNT: float
    RESCUE_VENT_SPEED: float

    def get_only_comp_weight(self) -> float:
        return (self.COMP_DEPTH + self.COMP_RATE + self.RECOIL + self.HAND_POSITION + self.CCF) * 100

    def get_only_vent_weight(self) -> float:
        return (self.VENT_VOL + self.VENT_SPEED + self.VENT_RATE + self.VENT_COUNT) * 100

    def get_rescue_vent_weight(self) -> float:
        return (self.VENT_VOL + self.VENT_SPEED + self.VENT_COUNT) * 100

    def get_vp_comp_weight(self, include_vent_speed: bool = False) -> float:
        # 분자(_overall_vent)에서 vent_speed를 가산하는 경우(infant)에는 분모에도 VENT_SPEED를 포함해야 한다.
        weight = self.VENT_VOL + self.VENT_COUNT + self.CCF
        if include_vent_speed:
            weight += self.VENT_SPEED
        return weight * 100

    def get_vp_vent_weight(self) -> float:
        return (self.COMP_DEPTH + self.COMP_RATE + self.RECOIL + self.HAND_POSITION + self.CCF + self.COMP_COUNT) * 100

    def weight_sum(self):
        return (
            self.COMP_DEPTH
            + self.COMP_RATE
            + self.RECOIL
            + self.HAND_POSITION
            + self.COMP_COUNT
            + self.CCF
            + self.VENT_VOL
            + self.VENT_COUNT
            + self.VENT_SPEED
            + self.RESCUE_VENT
            + self.VENT_RATE
        )


class ScoreWeightAdult(ScoreWeight):
    COMP_DEPTH = 0.20
    COMP_RATE = 0.15
    RECOIL = 0.15
    HAND_POSITION = 0.05
    COMP_COUNT = 0.05
    CCF = 0.25
    VENT_VOL = 0.1
    VENT_COUNT = 0.05
    VENT_SPEED = 0.0
    VENT_RATE = 0
    RESCUE_VENT = 0.0


class ScoreWeightChild(ScoreWeight):
    COMP_DEPTH = 0.20
    COMP_RATE = 0.15
    RECOIL = 0.15
    HAND_POSITION = 0.05
    COMP_COUNT = 0.05
    CCF = 0.25
    VENT_VOL = 0.1
    VENT_COUNT = 0.05
    VENT_SPEED = 0.0
    RESCUE_VENT = 0.0
    VENT_RATE = 0


class ScoreWeightInfant(ScoreWeight):
    COMP_DEPTH = 0.12
    COMP_RATE = 0.12
    RECOIL = 0.12
    HAND_POSITION = 0.06
    COMP_COUNT = 0.03
    CCF = 0.1
    VENT_VOL = 0.26
    VENT_COUNT = 0.1
    VENT_SPEED = 0.09
    VENT_RATE = 0
    RESCUE_VENT = 0.0


class ScoreWeightAdultCCO(ScoreWeight):
    COMP_DEPTH = 0.3
    COMP_RATE = 0.2
    RECOIL = 0.2
    HAND_POSITION = 0.1
    CCF = 0.2
    COMP_COUNT = 0.0
    VENT_VOL = 0.0
    VENT_COUNT = 0.0
    VENT_SPEED = 0.0
    VENT_RATE = 0
    RESCUE_VENT = 0.0


class ScoreWeightInfantCCO(ScoreWeight):
    COMP_DEPTH = 0.3
    COMP_RATE = 0.3
    RECOIL = 0.2
    HAND_POSITION = 0.1
    CCF = 0.1
    COMP_COUNT = 0.0
    VENT_VOL = 0.0
    VENT_COUNT = 0.0
    VENT_SPEED = 0.0
    VENT_RATE = 0
    RESCUE_VENT = 0.0


class ScoreWeightChildWithRescueVent(ScoreWeight):
    COMP_DEPTH = 0.16
    COMP_RATE = 0.12
    RECOIL = 0.12
    HAND_POSITION = 0.04
    COMP_COUNT = 0.04
    CCF = 0.2
    VENT_VOL = 0.08
    VENT_COUNT = 0.04
    VENT_SPEED = 0.0
    VENT_RATE = 0
    RESCUE_VENT = 0.2
    RESCUE_VENT_VOLUME = 0.2
    RESCUE_VENT_COUNT = 0.2
    RESCUE_VENT_SPEED = 0



class ScoreWeightInfantWithRescueVent(ScoreWeight):
    COMP_DEPTH = 0.11
    COMP_RATE = 0.11
    RECOIL = 0.1
    HAND_POSITION = 0.07
    COMP_COUNT = 0.04
    CCF = 0.07
    VENT_VOL = 0.18
    VENT_COUNT = 0.05
    VENT_SPEED = 0.07
    VENT_RATE = 0
    RESCUE_VENT = 0.2
    RESCUE_VENT_VOLUME = 0.2
    RESCUE_VENT_COUNT = 0.2
    RESCUE_VENT_SPEED = 0.2


class ScoreWeightVentOnly(ScoreWeight):
    COMP_DEPTH = 0
    COMP_RATE = 0
    RECOIL = 0
    HAND_POSITION = 0
    COMP_COUNT = 0
    CCF = 0
    VENT_VOL = 0.75
    VENT_RATE = 0.25
    VENT_COUNT = 0
    VENT_SPEED = 0
    RESCUE_VENT = 0


class ScoreWeightInfantVentOnly(ScoreWeight):
    COMP_DEPTH = 0
    COMP_RATE = 0
    RECOIL = 0
    HAND_POSITION = 0
    COMP_COUNT = 0
    CCF = 0
    VENT_VOL = 0.6
    VENT_RATE = 0.2
    VENT_COUNT = 0
    VENT_SPEED = 0.2
    RESCUE_VENT = 0


class ScoreWeightFactory:
    @staticmethod
    def create(target: str, training_type: str, guideline: str) -> "ScoreWeight":
        if training_type == "cpr":
            if target.lower() == "adult":
                return ScoreWeightAdult()
            elif target.lower() == "child" and guideline == "ERC2020":
                return ScoreWeightChildWithRescueVent()
            elif target.lower() == "child":
                return ScoreWeightAdult()
            elif target.lower() == "infant" and guideline == "ERC2020":
                return ScoreWeightInfantWithRescueVent()
            elif target.lower() == "child":
                return ScoreWeightChild()
            elif target.lower() == "infant":
                return ScoreWeightInfant()
        elif training_type == "compression_only":
            if target.lower() in ["adult", "child"]:
                return ScoreWeightAdultCCO()
            elif target.lower() == "infant":
                return ScoreWeightInfantCCO()
        elif training_type == "ventilation_only":
            if target.lower() in ["adult", "child"]:
                return ScoreWeightVentOnly()
            elif target.lower() == "infant":
                return ScoreWeightInfantVentOnly()
