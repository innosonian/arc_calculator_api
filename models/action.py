class Score:
    grade: int
    value: str | int
    criterion: str


class Action:
    compression_depth: list
    compression_rate: list
    compression_count: int
    hand_position: list
    ventilation_volume: list
    ventilation_speed: list
    ventilation_count: int
    action_type: str
    total_action_ms: int
    handsoff_ms: int
    cycle_cnt: int
    cycle_num: int


class ActionScore:
    comp_depth: Score | None
    comp_rate: Score | None
    recoil: Score | None
    hand_position: Score | None
    vent_vol: Score | None
    vent_speed: Score | None
    vent_rate: Score | None


class ActionWithScore:
    action_data: dict
    score: dict
    action_type: str
    actor: str

    def __init__(self, action_type: str, action_data: dict, score: dict, actor: str):
        self.action_type = action_type
        self.action_data = action_data
        self.score = score
        self.actor = actor
