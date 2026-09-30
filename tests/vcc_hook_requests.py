"""W4/W5 work-order texts formerly kept as production constants (HOOK_REQUESTS).

They were moved verbatim, unchanged, from mock_journey/course_state.py and
mock_journey/course_submission.py (P4, X3-18/S7-19): runtime code never used
them. The requested hooks are implemented (jobs.close_course_terminal,
reopen_course_recovery, finalize(completion_plan), seal checks) and covered by
behavior tests; tests/test_vcc_state.py, test_vcc_submission.py and
test_vcc_recovery.py still check that these historical texts name them.
"""

COURSE_STATE_HOOK_REQUESTS = """
W5 hook contract (vcc-internal-v1 / vcc-policy-v1). W5 owns integration_tests/test_vcc_*.py
and the mapping onto DynamoDB TransactWriteItems / get_item. Do not copy state.py _encode/_write.

CourseStore.get_item(key: {'PK': str, 'SK': str}) -> dict | None
  Consistent read of one control row. Python types (str/int/bool/list/dict/None), never Dynamo
  AttributeValue wrapping. Missing row is None. SDK/integrity failure -> CourseError TEMPORARILY_UNAVAILABLE.

CourseStore.transact(actions: list[dict]) -> bool
  Atomic write. True = all actions committed. False = condition or transaction conflict (retry).
  Other failures -> CourseError TEMPORARILY_UNAVAILABLE. Never partial apply.
  Duplicate PK+SK in one list is forbidden (repository refuses before this call).
  len(actions) <= CourseSettings.max_transaction_actions (fixture 20).
  W5 maps op=put to TransactWrite Put and op=condition_check to ConditionCheck.
  Conditions:
    if_not_exists: attribute_not_exists(PK)
    if_match: AND of #field = :value for each pair (merge into the Put; never extra Check on same key)
    if_missing_or_match: attribute_not_exists(PK) OR (all match pairs)
    if_greater (condition_check only, with if_match): AND #field > :value for each integer pair.
      Session checks use it for expires_at > commit-time now (v1 _session_condition parity).
  Session/USER rows keep existing PK/SK (SESSION#id/AUTH, USER#principal/STATE).

CourseBlobStore.put_bytes(body: bytes) -> str
  Immutable private object, return sha256 hex. Put BEFORE the control transact. Failed commit
  leaves an unreferenced object; do not auto-delete (T4). get_bytes(digest) -> bytes | None.

ensure_epoch(auth, binding) -> GateView
  Read USER epoch + COURSE HEAD + FINAL for that epoch. Create HEAD(waiting, empty completions)
  and FINAL(free) together. Both present: verify original IDs/hash, no-op. One missing: 503.
  ITEM is not created. Errors: SESSION_*, TEMPORARILY_UNAVAILABLE.

begin_inventory(auth, learner) -> InventoryTicket
  CAS inventory_generation += 1 on COURSE_LEARNER#learner_key / EPOCH#epoch#HEAD.
  Missing learner HEAD: create generation=1 waiting. Failure: SESSION_*, 503.

apply_inventory(auth, ticket, result_or_error) -> InventoryView
  Apply only ticket.generation == current and ticket.epoch == USER epoch. Stale success/fail: no-op.
  CourseError: learner waiting (arc_progress_unavailable or contract_pending); do not fan-out
  delete course rows. tuple[AssignmentBinding]: ready, including empty (V06). Parent of refresh.

begin_refresh(auth, binding, inventory: InventoryTicket) -> RefreshTicket
  After ensure_epoch. CAS HEAD.refresh_generation += 1. Keep ready until apply confirms failure.
  Ticket stores parent inventory_generation. Uninitialized stays waiting.

apply_refresh(auth, ticket, bundle_or_error) -> GateView
  Match ticket.generation, USER epoch, parent inventory_generation. Parent generation change: no-op.
  CourseError: waiting. CourseBundle full reconcile success: ready + bundle_ref. Value conflict
  (source_progress vs local completed set): reconciliation_required, keep local evidence.

load_view(auth, ids) / load_start_view(auth, command) -> CourseView
  Auth + ownership + digest re-check. No enrollment/calc/submit writes.

find_created(auth, command, *, kind) -> StartReceipt | None
  SESSION#id / COURSE_CREATE#request_id (not CREATE#). Current auth+ownership+digest first.
  Same digest: stored receipt. Different body: IDEMPOTENCY_CONFLICT. Do not gate-block replay.

start(auth, command, *, kind, view, template) -> StartReceipt
  Replay COURSE_CREATE before can_start. New start CAS: session/user/learner ready+assignment,
  HEAD revision/epoch/ready/hash, training ITEM missing-or-not-complete, FINAL free when
  role=final_assessment. content: START+locator, start_id only. attempt: ATTEMPT, attempt_id only.
  Max 4 conflicts then TEMPORARILY_UNAVAILABLE. No partial commit.

report(...) -> StoredProgressReceipt
  Locator COURSE_START#start_id/META then original-epoch START. Bound session, version, digest.
  Same digest: immutable stored receipt. Different body: 409. historical_only when START.epoch !=
  USER epoch (isCompleted/isPassed/courseStatus all null; no current HEAD/ITEM/FINAL writes).
  Else REPORT+START+ITEM+HEAD atomic. Gate waiting still allows existing start reports.

Race schedules for integration_tests (W5):
  V06 inventory q1-success after q2-fail; refresh q1-success after q2-fail; parent inventory
  generation bump then stale bundle; inventory fail vs new start on same learner CAS.
  V07 create commit, drop response, set gate waiting, replay same session/id/body.
  V08 HEAD+FINAL init vs one-row corruption 503; real-student logout keeps epoch.
  V10 two incomplete training starts allowed; complete-then-start 409; start-then-complete keeps attempt.
  V11 two final starts, one wins FINAL.phase=active.
  V16/V17/V20 previous-epoch report/finalize does not touch current HEAD/ITEM/FINAL.
  V22 4 conflicts -> 503 and write count 0; action keys unique; actions <= 20.
"""


COURSE_SUBMISSION_HOOK_REQUESTS = """
W5 hooks requested by W4. Do not implement in W4 files. Keep one existing finalize
transaction; never Mock-slot commit then course commit. V IDs: V12, V18, V19, V20, V21.

1) optional course_binding on the existing attempt template whitelist
   signature: AuthManager.prepare_resume(template) and DynamoStateRepository.create_attempt
              continue to require today's _TEMPLATE_FIELDS. Add optional
              template['course_binding'] only when type is CourseBinding (or its
              frozen field dict) and branch the whitelist. Do not make the field
              required on retained rows. Projection exact compare of the six-key
              condition is unchanged.
   conditions: CourseCalculationBridge.prepare already froze existing_template_json
               as 7-key execution + mapping_version, without resume secrets and
               without ExecutionCatalog.get_definition(course_id).
   errors: PROFILE_MISMATCH on condition drift; CALCULATOR_CONTRACT_MISMATCH on
           5-key definitions; EXECUTION_DEFINITION_MISSING/UNSUPPORTED from prepare.
   V IDs: V18.

2) jobs.finalize injects CourseCompletionPlan.build into the single existing transaction
   signature: DynamoJobRepository.finalize(job_id, owner, fence, final_ref, evaluation,
              chart_publication, *, completion_plan: CourseCompletionPlan | None = None)
              When attempt.course_binding is missing, keep the current legacy branch
              (WritePlan has no COURSE# / SUBMISSION# writes). When bound, merge
              plan.actions_json writes/conditions with the existing JOB/ATTEMPT/USER
              CAS. Do not call a second commit. Do not create an executable submit
              queue or OUTBOX item for ARC. DisabledArcGateway is not invoked from
              finalize in the launch version.
   conditions: owner/fence/lease/candidate/chart publication and USER epoch stay as
               today. Course ITEM/HEAD/FINAL and SUBMISSION#<attempt>/RESULT#<digest>
               join the same TransactWrite. Previous attempt.epoch != user.epoch
               must not Put/Update current COURSE HEAD/ITEM/FINAL. max actions stay
               inside CourseSettings.max_transaction_actions (fixture finalize=7).
               rows['bundle'] must be the verified CourseBundle selected by HEAD.bundle_ref.
               HEAD progress_json/completed_placements/course_complete are one projection.
               Conditions additionally bind head_definition_hash/final_phase/final_active_attempt_id.
               Replacement final definitions retain original ITEM and enrollment Pass while
               current-view completion stays unresolved; no Pass is copied to a new item.
   errors: TEMPORARILY_UNAVAILABLE after max_conflict_retries; CALCULATOR_CONTRACT_MISMATCH
           on evaluation shape; existing JobLeaseLost on stale fence.
   V IDs: V19, V20, V21.

3) DynamoJobRepository.close_course_terminal(job_id, owner, fence, evidence)
   signature: close_course_terminal(self, job_id: str, owner: object, fence: object,
              evidence: RecoveryEvidence) -> object
   conditions: only when evidence.action == 'close_terminal' and every T5 predicate
               already inspected (proven local terminal error, trusted binding/input,
               current owner/fence/lease, no committed result, candidate search
               complete, no recoverable candidate). Same transaction: JOB
               failed+terminal_seal+evidence_digest+owner=null+due removed; ATTEMPT
               failed/evaluation=null with file refs preserved; original-epoch FINAL
               free and active_attempt_id=null only for the self-reference; HEAD
               revision++. Completion set and scores unchanged. Seal fields:
               job_id, attempt_id, epoch, call_id, fence, evidence_digest.
               evidence_digest = typed.digest of the RecoveryEvidence identity
               (job_id, attempt_id, revisions, epoch, call_id, fence, input_digest,
               stage, candidate_state, code, action). Same seal re-call is a no-op
               and must not write a newer FINAL. Previous-epoch closure must not
               read/write current USER epoch HEAD/ITEM/FINAL.
   errors: INVALID_STATE when predicates fail; TEMPORARILY_UNAVAILABLE on conflict;
           never interpret failed/error_code/lease expiry/active_counted=false/app 30s
           as permission to free FINAL.
   V IDs: V12, V19.

4) DynamoJobRepository.reopen_course_recovery(job_id, evidence)
   signature: reopen_course_recovery(self, job_id: str, evidence: RecoveryEvidence) -> object
   conditions: exact input + valid candidate + adapter verify, no result, FINAL still
               self-references this attempt, JOB/ATTEMPT/HEAD/FINAL revisions match.
               Preserve failure history and create recovery due. Next claim issues a
               new owner/lease and higher fence then resume_candidate (recalc 0).
               Not for sealed jobs, changed input, or retained Mock failed rows
               without a valid candidate.
   errors: INVALID_STATE otherwise. Do not auto-reopen from claim().
   V IDs: V12, V19.

5) seal checks on claim / begin_calculation / mark_calculation_saved / finalize
   signature: each existing method; if job.terminal_seal is set, refuse mutation
              except close_course_terminal no-op on the same seal.
   conditions: sealed job has no automatic reopen. Late finalize after seal does
               not change current FINAL. Old worker fence cannot free a new FINAL.
   errors: INVALID_STATE or JobLeaseLost. CALCULATION_OUTCOME_UNKNOWN stays
           recovery_required, not close_terminal.
   V IDs: V12, V19.
"""
