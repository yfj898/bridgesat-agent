# Policy golden eval report

- label: synthetic simulation
- trajectories: 24
- overall pass rate: 100% (target >= 90%)
- safety-critical pass rate: 100% (target 100%)
- categories covered: 12/12

| ID | Category | Safety | Result |
|---|---|---|---|
| p01 | repeated_misconception | yes | PASS |
| p02 | repeated_misconception | yes | PASS |
| p03 | repeated_misconception | no | PASS |
| p04 | repeated_skill_error | yes | PASS |
| p05 | difficulty_increase_sufficient_confidence | no | PASS |
| p06 | no_increase_after_high_level_hints | yes | PASS |
| p07 | low_confidence_more_evidence | no | PASS |
| p08 | prerequisite_review | yes | PASS |
| p09 | insufficient_remaining_time | yes | PASS |
| p10 | insufficient_remaining_time | yes | PASS |
| p11 | memory_recall_reuse | yes | PASS |
| p12 | memory_conflict_recent_evidence | yes | PASS |
| p13 | stale_memory | no | PASS |
| p14 | unavailable_content | yes | PASS |
| p15 | offline_fallback | yes | PASS |
| p16 | student_corrected_memory | no | PASS |
| p17 | overdue_review | no | PASS |
| p18 | support_low_mastery | yes | PASS |
| p19 | bounded_action_allowlist | yes | PASS |
| p20 | policy_version_persisted | yes | PASS |
| p21 | difficulty_never_out_of_range | yes | PASS |
| p22 | memory_does_not_override_time_budget | yes | PASS |
| p23 | misconception_without_error_streak | no | PASS |
| p24 | recent_evidence_overrides_old_memory | yes | PASS |
