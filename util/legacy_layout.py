"""The legacy private object layout (D28) as pure rules: no SDK, clock or I/O.

  {directory}/{stage}/{org}/{UTC date of the stem}/{stem}.bin        raw CPR binary
                                                        .meta.json  raw input meta
                                                        .aed.bin    AED binary (when present)
                                                        .json       chart dataset

util/uploader.py is the ported original and keeps its own literals and
functions (the deployment preflight reads its BUCKET/RTDATA_DIRECTORY by AST).
This module must not import it: the uploader imports boto3. Bindings pass the
UTC date they computed with their own ``date_prefix``. How meta/chart bodies
are serialized stays with each binding.
"""

import re


RAW_SUFFIX = ".bin"
META_SUFFIX = ".meta.json"
AED_SUFFIX = ".aed.bin"
CHART_SUFFIX = ".json"
# The stem util.uploader.build_key_stem generates: epoch seconds and a UUID.
KEY_STEM = re.compile(r"CPR-ACTION-([0-9]{10})-([0-9a-f-]{36})\Z")


def stage_prefix(directory, stage):
    """``{directory}/{stage}/``: the namespace every raw and chart key starts with."""
    return f"{directory}/{stage}/"


def object_base(directory, stage, org, date, key_stem):
    """The shared path of one measurement's raw, meta, AED and chart objects (no suffix)."""
    return f"{directory}/{stage}/{org}/{date}/{key_stem}"
