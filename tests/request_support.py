"""Shared request builders moved out of test modules (X4-04).

Kept separate from ``tests/_synth.py``: that file is the reviewed pure generator
whose SHA-256 is pinned as ``generator_sha256`` in the reference-parity evidence
manifests, so it must stay byte-identical.
"""

import base64
from urllib.parse import quote, urlencode


# 검출 완결성 시험의 dict 조건(tests/test_detection_completion.py 에서 그대로 옮김).
def condition(target, training, guideline="ARC2025"):
    return {"mode": "training", "target": target, "training_type": training, "guideline": guideline,
            "cpr_cycle_type": "152" if target == "infant" else "302", "is_2rescuers": False}


# 참조 구현의 urlencoded(base64) 요청 이벤트(tests/test_reference_http_contract.py 의 _form_event 를 그대로 옮김).
def form_event(fields):
    # The reference decodes URL quoting twice: unquote, then parse_qs.
    encoded = quote(urlencode(fields), safe="")
    return {
        "httpMethod": "POST",
        "path": "/cpr-analysis",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "isBase64Encoded": True,
        "body": base64.urlsafe_b64encode(encoded.encode()).decode(),
    }
