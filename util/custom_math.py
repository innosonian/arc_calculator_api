# 원본 hstm_v2 util/custom_math.py:1-7 그대로 이식(구현 변경 금지 — V1 패리티 보존).
"""
The existing round() function has an issue.
round(1.5) = 2 and round(2.5) = 2, which is not accurate.
Therefore, a custom function is created to perform accurate rounding.
"""


# 비음수 입력 전제(B-9, 스펙 §5.8): int(number + 0.5)는 0 이상 입력에서만 사사오입 반올림이다.
# 음수에서는 수학적 반올림과 다르게 동작한다(예: custom_round(-1.5) == -1, custom_round(-0.7) == 0).
# 이 저장소의 모든 호출부(점수 0~100, 깊이/환기량/시간 파생값)는 비음수만 전달한다.
def custom_round(number):
    return int(number + 0.5)
