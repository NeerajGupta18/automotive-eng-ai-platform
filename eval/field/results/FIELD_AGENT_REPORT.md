# Field-issue agent: evaluation

27 labelled cases · generated 2026-10-22T05:01 UTC · recordings replayed on the compiled C code (SIL)

| Metric | Value | Threshold |
|---|---|---|
| root_cause_accuracy | 1.0 | ≥ 0.95 |
| symptom_accuracy | 1.0 | ≥ 0.95 |
| reproduction_rate | 1.0 | ≥ 0.95 |
| fix_success_rate | 1.0 | ≥ 0.9 |
| false_defects | 0 | 0 |
| missed_defects | 0 |  |
| mean_seconds_per_case | 1.87 |  |

| Root cause | Cases | Correct |
|---|---|---|
| calibration_regression | 8 | 8 |
| sensor_false_target | 4 | 4 |
| correct_intervention | 4 | 4 |
| system_limitation | 4 | 4 |
| implementation_defect | 4 | 4 |
| insufficient_data | 3 | 3 |

| Case | Build | Expected | Agent | Symptom | Culprit | Fix | Reproduced | s |
|---|---|---|---|---|---|---|---|---|
| E01 | 2.4.0 | calibration_regression | ✅ calibration_regression | unexpected_braking | confirm_cycles | confirm_cycles 5→15 ✅ | yes | 3.14 |
| E02 | 2.4.0 | calibration_regression | ✅ calibration_regression | unexpected_braking | confirm_cycles | confirm_cycles 5→11 ✅ | yes | 3.62 |
| E03 | 2.4.0 | calibration_regression | ✅ calibration_regression | unexpected_braking | confirm_cycles | confirm_cycles 5→13 ✅ | yes | 3.13 |
| E04 | 2.4.0 | calibration_regression | ✅ calibration_regression | unexpected_braking | confirm_cycles | confirm_cycles 5→16 ✅ | yes | 3.21 |
| E05 | 2.4.0 | calibration_regression | ✅ calibration_regression | false_warning | confirm_cycles | confirm_cycles 5→13 ✅ | yes | 3.07 |
| E06 | 2.4.0 | calibration_regression | ✅ calibration_regression | unexpected_braking | confirm_cycles | confirm_cycles 5→12 ✅ | yes | 3.39 |
| E07 | 2.4.0 | calibration_regression | ✅ calibration_regression | unexpected_braking | confirm_cycles | confirm_cycles 5→14 ✅ | yes | 3.06 |
| S01 | 2.3.0 | sensor_false_target | ✅ sensor_false_target | unexpected_braking |  |  | yes | 4.24 |
| S02 | 2.3.0 | sensor_false_target | ✅ sensor_false_target | unexpected_braking |  |  | yes | 4.5 |
| S03 | 2.4.0 | sensor_false_target | ✅ sensor_false_target | unexpected_braking |  |  | yes | 4.55 |
| S04 | 2.3.0 | sensor_false_target | ✅ sensor_false_target | unexpected_braking |  |  | yes | 4.35 |
| C01 | 2.3.0 | correct_intervention | ✅ correct_intervention | unexpected_braking |  |  | yes | 0.36 |
| C02 | 2.4.0 | correct_intervention | ✅ correct_intervention | unexpected_braking |  |  | yes | 0.39 |
| C03 | 2.3.0 | correct_intervention | ✅ correct_intervention | unexpected_braking |  |  | yes | 0.39 |
| C04 | 2.3.0 | correct_intervention | ✅ correct_intervention | unexpected_braking |  |  | yes | 0.35 |
| L01 | 2.3.0 | system_limitation | ✅ system_limitation | no_braking |  |  | yes | 0.36 |
| L02 | 2.3.0 | system_limitation | ✅ system_limitation | no_braking |  |  | yes | 0.37 |
| L03 | 2.3.0 | system_limitation | ✅ system_limitation | no_braking |  |  | yes | 0.39 |
| L04 | 2.3.0 | system_limitation | ✅ system_limitation | no_braking |  |  | yes | 0.36 |
| D01 | 2.4.1-pilot | implementation_defect | ✅ implementation_defect | moved_after_stop |  | rollback to 2.3.0 ✅ | yes | 1.49 |
| D02 | 2.4.1-pilot | implementation_defect | ✅ implementation_defect | moved_after_stop |  | rollback to 2.3.0 ✅ | yes | 1.32 |
| D03 | 2.4.2-pilot | implementation_defect | ✅ implementation_defect | unexpected_braking |  | rollback to 2.3.0 ✅ | yes | 1.19 |
| D04 | 2.4.3-pilot | implementation_defect | ✅ implementation_defect | no_braking |  | rollback to 2.3.0 ✅ | yes | 1.12 |
| R01 | 2.5.0-pilot | calibration_regression | ✅ calibration_regression | unexpected_braking | v_max_mps | validate (prevented collisions) ✅ | yes | 0.92 |
| I01 | 2.3.0 | insufficient_data | ✅ insufficient_data | unexpected_braking |  |  |  | 0.38 |
| I02 | 2.3.0 | insufficient_data | ✅ insufficient_data | unexpected_braking |  |  |  | 0.4 |
| I03 | 2.4.0 | insufficient_data | ✅ insufficient_data | unexpected_braking |  |  |  | 0.37 |

Synthetic cases generated from known causes (scripts/make_field_data.py); measures the investigation chain end to end, not accuracy on real fleet data.
