You are now REPAIRING a LessonScene file that failed. Everything in the contract above still applies.
- Fix the root cause named by the error class and traceback. Keep the rest of the animation the same.
- Return the COMPLETE corrected file (not a diff) in the "code" field.
- Do not write SEGMENT_DURATIONS / SEGMENT_ENDS; the pipeline re-injects them.
- Never "fix" an error by deleting the segment's content or the sync_to calls.
---USER---
Repair attempt {{attempt}} of {{max_repairs}}.

Error class: {{error_class}}
What this usually means: {{hint}}

Error output (trimmed; line numbers refer to the numbered code below):
```
{{error}}
```

Current code:
```
{{numbered_code}}
```

Timing reminder: N = {{n_segments}} segments, SEGMENT_DURATIONS = {{durations}}, total {{total}}s.
Return the full corrected file.
