# 원본 hstm_v2 util/uploader.py 이식.
# 변경(스펙 §3): S3 prefix hstm_v2 → arc, env HSTM_STORAGE_REGION → ARC_STORAGE_REGION(기본 ap-northeast-2 유지).
# 그 외 동작(STAGE==test skip, 경로 규칙 {directory}/{stage}/{org}/{UTC날짜}/{stem}.*, meta 처리)은 원본 그대로.
# 검토 반영 2026-09-05: P8 upload_json_file 의 직렬화 예외 포착을 (TypeError, ValueError)로 —
# JSONDecodeError 는 ValueError 하위라 포함되고, 순환참조 ValueError 도 잡힌다. 불필요 import 제거.
import datetime
import json
import os
import re
import uuid

from boto3 import client

BUCKET = "brayden-online-v2-api-storage"
RTDATA_DIRECTORY = "calculator_result/interpreted_rtdata/arc"
BUCKET_REGION = os.getenv("ARC_STORAGE_REGION", "ap-northeast-2")

# org_id가 empty/null/형식이상일 때 경로에 쓰는 sentinel
NO_ORG = "_no_org"
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def org_prefix(org_id: str | None) -> str:
    """org_id가 UUID 형태면 그대로 경로 prefix로, empty/null/형식이상이면 sentinel(_no_org).

    이름 등 PII는 경로(key)에 넣지 않고 org_id(UUID, 비-PII)로만 분할한다.
    """
    org_id = (org_id or "").strip()
    return org_id if _UUID_RE.fullmatch(org_id) else NO_ORG


def build_key_stem() -> str:
    """차트·바이너리·메타가 공유하는 파일명 stem (1:1 연결용)."""
    now = datetime.datetime.now(datetime.UTC)
    return f"CPR-ACTION-{int(now.timestamp())}-{uuid.uuid4()}"


def date_prefix(key_stem: str) -> str:
    """stem에 박힌 epoch의 UTC 날짜(YYYY-MM-DD) — 날짜별 디렉토리 분할용.

    파일명 타임스탬프에서 날짜를 뽑으므로 디렉토리와 파일명이 항상 일치한다.
    epoch가 없는 stem이면 현재 UTC 날짜로 폴백.
    """
    m = re.search(r"-(\d{10})-", f"-{key_stem}-")
    ts = int(m.group(1)) if m else int(datetime.datetime.now(datetime.UTC).timestamp())
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%d")


def build_raw_input_meta(condition: dict | None, vp_event_list, organization: dict | None) -> dict:
    """원본 입력 재현·식별용 메타.

    Organization이 없거나 null이어도, org_name/first_name/last_name/org_id가
    empty/null이어도 전부 null로 처리한다(정보 손실 없이 본문에 보존).
    """
    org = organization or {}
    return {
        "org_id": (org.get("org_id") or None),
        "org_name": (org.get("org_name") or None),
        "first_name": (org.get("First_name") or None),
        "last_name": (org.get("Last_name") or None),
        "condition": condition or None,
        "vp_event_list": vp_event_list or [],
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def upload_json_file(
    data: list[dict] | dict,
    directory: str = RTDATA_DIRECTORY,
    stage: str = "prod",
    key_stem: str | None = None,
    org: str | None = None,
) -> str | None:
    """
    :param data: must be a data type that can be converted to JSON
    :param directory: file upload directory
    :param stage: stage
    :param key_stem: 차트·바이너리·메타가 공유하는 stem. 미지정 시 새로 생성
    :param org: org_id prefix (None이면 _no_org)
    """
    if stage == "test":
        return

    try:
        body = json.dumps(data)
    except (TypeError, ValueError):  # 검토 반영 2026-09-05: P8 (JSONDecodeError ⊂ ValueError, 순환참조 포함)
        return

    if key_stem is None:
        key_stem = build_key_stem()
    org = org or NO_ORG

    s3_client = client("s3", region_name=BUCKET_REGION)
    path = f"{directory}/{stage}/{org}/{date_prefix(key_stem)}/{key_stem}.json"

    s3_client.put_object(Bucket=BUCKET, Key=path, Body=body)

    return path


def upload_raw_input(
    cpr_bytes: bytes,
    aed_bytes: bytes,
    meta: dict,
    stage: str = "prod",
    key_stem: str | None = None,
    org: str | None = None,
    directory: str = RTDATA_DIRECTORY,
) -> str | None:
    """원본 바이너리 + 메타를 차트와 동일 경로/stem으로 저장한다.

    {directory}/{stage}/{org}/{UTC날짜}/{stem}.bin       원본 CPR 바이너리
    {directory}/{stage}/{org}/{UTC날짜}/{stem}.meta.json condition/vp_event + org/이름(PII)
    {directory}/{stage}/{org}/{UTC날짜}/{stem}.aed.bin    (AED 데이터 있을 때만)
    """
    if stage == "test":
        return

    if key_stem is None:
        key_stem = build_key_stem()
    org = org or NO_ORG

    s3_client = client("s3", region_name=BUCKET_REGION)
    base = f"{directory}/{stage}/{org}/{date_prefix(key_stem)}/{key_stem}"

    s3_client.put_object(Bucket=BUCKET, Key=f"{base}.bin", Body=cpr_bytes or b"")
    s3_client.put_object(Bucket=BUCKET, Key=f"{base}.meta.json", Body=json.dumps(meta, default=str))
    if aed_bytes:
        s3_client.put_object(Bucket=BUCKET, Key=f"{base}.aed.bin", Body=aed_bytes)

    return base


def create_signed_url(
    key: str | None,
    expires_in: int = 300,
) -> str | None:
    if not key:
        return None
    s3_client = client("s3", region_name=BUCKET_REGION)
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )
