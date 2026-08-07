# Offline and synchronization eval report

- label: controlled internal test
- scenarios: 10
- pass rate: 100%

| Scenario | Target | Result |
|---|---|---|
| full_offline_session | offline core-flow completion = 100% | PASS (accepted=6/6 evidence=2) |
| refresh_recovery | duplicate scoring incidents = 0 | PASS (duplicates=6 scored_valid=2) |
| server_restart | restart recovery = 100% | PASS (version=7 skills=2) |
| duplicate_batch_upload | duplicate scoring incidents = 0 | PASS (duplicate=['evt_a1'] accepted=[]) |
| out_of_order_upload | known-version scoring consistency = 100% | PASS (first=['MISSING_DEPENDENCY'] then accepted=['evt_2']) |
| late_event_after_summary | unacknowledged-event loss = 0 | PASS (accepted=['evt_3'] conflicts=['SUMMARY_REVISED'] state=SESSION_COMPLETED) |
| old_known_content_version | known-version scoring consistency = 100% | PASS (code=['QUESTION_VERSION_UNKNOWN']) |
| unknown_content_version | known-version scoring consistency = 100% | PASS (code=['QUESTION_VERSION_UNKNOWN']) |
| parallel_device_branches | duplicate scoring incidents = 0 | PASS (weights=[1.0, 0.5] conflicts=['PARALLEL_ATTEMPT_DETECTED']) |
| pending_event_retention_after_failure | unacknowledged-event loss = 0 | PASS (first=['INVALID_SCHEMA'] stored_before_retry=0 then accepted=['evt_retry']) |
