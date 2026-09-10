# 원본 hstm_v2 models/action.py (V1 패리티 보존)
# 스펙 §4.5(I-1): 원본의 Score/Action/ActionScore(어노테이션-전용, 저장소 전체 소비처 0건 grep 재확인)는 미이식.
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
