"""DynamoDB PK/SK row keys of the journey table (ARCHITECTURE §4), as pure functions.

Every stored row key is built here with the same f-string formatting the rows
were written with; changing a format makes existing rows unreachable. The
functions return {"PK", "SK"} dicts in that order. Callers that keep a
tuple/list form (course_submission write plans) convert at the call site.
due_partition is the GSI1 partition of due JOB/OUTBOX rows. Row keys of other
tables or namespaces (RELAY_SCAN#, OPS#) are built by their own modules with
aws_scope.environment_namespace.

This module imports nothing from the repository, so course_state keeps its rule
of not importing state.py helpers while sharing the key layout with state.py
and jobs.py.
"""


def row_key(kind, value, suffix):
    """``{kind}#{value}`` / ``suffix``: the generic USER/SESSION/ATTEMPT/JOB/OUTBOX row key."""
    return {"PK": f"{kind}#{value}", "SK": suffix}


def session_key(session_id):
    return {"PK": f"SESSION#{session_id}", "SK": "AUTH"}


def user_key(principal):
    return {"PK": f"USER#{principal}", "SK": "STATE"}


def attempt_key(attempt_id):
    return {"PK": f"ATTEMPT#{attempt_id}", "SK": "META"}


def job_key(job_id):
    return {"PK": f"JOB#{job_id}", "SK": "STATE"}


def outbox_key(job_id):
    return {"PK": f"OUTBOX#{job_id}", "SK": "DISPATCH"}


def course_pk(scope_key):
    return f"COURSE#{scope_key}"


def course_head_key(scope_key, epoch):
    return {"PK": f"COURSE#{scope_key}", "SK": f"EPOCH#{epoch}#HEAD"}


def course_final_key(scope_key, epoch):
    return {"PK": f"COURSE#{scope_key}", "SK": f"EPOCH#{epoch}#FINAL"}


def course_item_key(scope_key, epoch, placement_key):
    return {"PK": f"COURSE#{scope_key}", "SK": f"EPOCH#{epoch}#ITEM#{placement_key}"}


def course_start_key(scope_key, epoch, start_id):
    return {"PK": f"COURSE#{scope_key}", "SK": f"EPOCH#{epoch}#START#{start_id}"}


def course_report_key(scope_key, epoch, start_id, report_id):
    return {"PK": f"COURSE#{scope_key}", "SK": f"EPOCH#{epoch}#REPORT#{start_id}#{report_id}"}


def learner_head_key(learner_key, epoch):
    return {"PK": f"COURSE_LEARNER#{learner_key}", "SK": f"EPOCH#{epoch}#HEAD"}


def start_locator_key(start_id):
    return {"PK": f"COURSE_START#{start_id}", "SK": "META"}


def create_receipt_key(session_id, request_id):
    return {"PK": f"SESSION#{session_id}", "SK": f"COURSE_CREATE#{request_id}"}


def principal_locator_key(principal, epoch):
    return {"PK": f"COURSE_PRINCIPAL#{principal}", "SK": f"EPOCH#{epoch}"}


def submission_key(attempt_id, result_digest):
    return {"PK": f"SUBMISSION#{attempt_id}", "SK": f"RESULT#{result_digest}"}


def due_partition(kind):
    """GSI1PK of a due JOB/OUTBOX row (the Relay's due index)."""
    return f"DUE#{kind}"


def pair(key):
    """(PK, SK) tuple form of a row key, as write plans record affected keys."""
    return key["PK"], key["SK"]
