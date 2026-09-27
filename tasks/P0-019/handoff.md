# P0-019 integration regression

TeamFactory V1 38 passed; V2 181 passed/1 failed. The failing trace fixture inserted an
unknown ALL-CAPS identifier into its query yet expected source-backed post-stop policy calls.
The intentional P1-001E evidence gate now correctly returns no evidence for that query.
Do not relax the retrieval gate or secrecy assertions: cover both source-backed lowercase
query marker and explicit-identifier insufficient path; assert canary redaction in both.
Only tests/test_trace_contract.py will change. Existing source/evidence remains preserved.
After focused diagnosis, run original task V1/V2 and close. Maximum three fix cycles.
