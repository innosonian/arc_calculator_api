# 원본: hstm_v2 tests/test_raw_input_meta.py (ARC 각색 이식 — 순수 로직 계약은 원본 그대로,
# S3 경로 prefix의 arc 반영(스펙 §3: calculator_result/interpreted_rtdata/arc) 검증 추가)
"""원본 입력 저장의 순수 로직(org_prefix / date_prefix / build_raw_input_meta) 회귀 테스트.

S3 업로드는 타지 않고, 경로 prefix 정규화와 메타의 null 처리만 검증한다.
"""

import re

from util.uploader import NO_ORG, RTDATA_DIRECTORY, build_key_stem, build_raw_input_meta, date_prefix, org_prefix


class TestArcStoragePrefix:
    def test_rtdata_directory_is_arc(self):
        # 스펙 §3: S3 prefix는 calculator_result/interpreted_rtdata/arc (신규 데이터부터 arc prefix).
        assert RTDATA_DIRECTORY == "calculator_result/interpreted_rtdata/arc"


class TestOrgPrefix:
    def test_valid_uuid_used_as_prefix(self):
        uid = "285b5dfe-8a16-e911-b189-005056b10657"
        assert org_prefix(uid) == uid

    def test_uppercase_uuid_ok(self):
        uid = "285B5DFE-8A16-E911-B189-005056B10657"
        assert org_prefix(uid) == uid

    def test_empty_string_to_sentinel(self):
        assert org_prefix("") == NO_ORG

    def test_none_to_sentinel(self):
        assert org_prefix(None) == NO_ORG

    def test_whitespace_to_sentinel(self):
        assert org_prefix("   ") == NO_ORG

    def test_non_uuid_to_sentinel(self):
        assert org_prefix("Quality Care Health System") == NO_ORG

    def test_path_traversal_to_sentinel(self):
        # 경로 안전: 비정상 값은 sentinel로 흡수
        assert org_prefix("../../etc") == NO_ORG


class TestDatePrefix:
    def test_epoch_to_utc_date(self):
        # 1781013077 = 2026-06-09T13:51:17Z
        assert date_prefix("CPR-ACTION-1781013077-7e92414e-5b1f-497d-b01e-b266d4b53406") == "2026-06-09"

    def test_utc_midnight_boundary(self):
        # 1781049599 = 2026-06-09T23:59:59Z — KST(+9)로는 06-10 아침이지만 UTC 날짜를 쓴다
        assert date_prefix("CPR-ACTION-1781049599-x") == "2026-06-09"
        assert date_prefix("CPR-ACTION-1781049600-x") == "2026-06-10"  # 00:00:00Z

    def test_stem_without_epoch_falls_back_to_today(self):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_prefix("no-epoch-here"))

    def test_built_stem_matches_its_own_date(self):
        stem = build_key_stem()
        epoch = int(stem.split("-")[2])
        from datetime import datetime, timezone

        assert date_prefix(stem) == datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


class TestBuildRawInputMeta:
    def test_full_organization(self):
        org = {
            "show_welcome_screen": True,
            "Last_name": "Russo",
            "First_name": "Bode",
            "org_name": "Quality Care Health System",
            "org_id": "285b5dfe-8a16-e911-b189-005056b10657",
        }
        cond = {"target": "adult", "is_2rescuers": True}
        meta = build_raw_input_meta(cond, [{"event": 10, "timestamp": 1}], org)
        assert meta["org_id"] == "285b5dfe-8a16-e911-b189-005056b10657"
        assert meta["org_name"] == "Quality Care Health System"
        assert meta["first_name"] == "Bode"
        assert meta["last_name"] == "Russo"
        assert meta["condition"] == cond
        assert meta["vp_event_list"] == [{"event": 10, "timestamp": 1}]
        assert meta["created_at"]

    def test_organization_none(self):
        # 요청바디에 Organization이 없거나 null인 경우
        meta = build_raw_input_meta({"target": "adult"}, [], None)
        assert meta["org_id"] is None
        assert meta["org_name"] is None
        assert meta["first_name"] is None
        assert meta["last_name"] is None
        assert meta["vp_event_list"] == []

    def test_org_fields_empty_or_missing_become_null(self):
        # org_name/first/last/org_id가 empty string이거나 키 자체가 없을 때 전부 null
        org = {"org_id": "", "org_name": "", "First_name": "", "Last_name": ""}
        meta = build_raw_input_meta(None, None, org)
        assert meta["org_id"] is None
        assert meta["org_name"] is None
        assert meta["first_name"] is None
        assert meta["last_name"] is None
        assert meta["condition"] is None
        assert meta["vp_event_list"] == []

    def test_partial_organization(self):
        # 일부만 있는 경우 — 있는 값은 보존, 없는 값은 null
        org = {"org_name": "Quality Care Health System"}
        meta = build_raw_input_meta({"target": "infant"}, [], org)
        assert meta["org_name"] == "Quality Care Health System"
        assert meta["org_id"] is None
        assert meta["first_name"] is None
        assert meta["last_name"] is None
