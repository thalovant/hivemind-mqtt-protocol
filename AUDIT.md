Last Edit: Codex (GPT-6) - 2026-09-30 - Motive: Add missing license files and document verified repository languages.

# Audit

## 2026-09-30: license and language metadata

- Fixed: missing root license text. Added [Apache-2.0](LICENSE), based on Existing [pyproject.toml](pyproject.toml) declaration.
- Manual review: GitHub already reports Python; no language override is necessary. Source evidence: [`HiveMindMqttProtocol`](hivemind_mqtt_protocol/__init__.py#L70), [`HiveMindMqttProtocol._cfg`](hivemind_mqtt_protocol/__init__.py#L107).
- Baseline verification: `python -m pytest tests -q --maxfail=1 --ignore=tests/e2e --ignore=tests/test_integration_live_stack.py` — 80 passed, 1 warning in 1.17s The broader baseline run failed in the installed hivescope integration harness; the unit-only rerun passed.
- Test evidence: [tests/e2e/test_mqtt_e2e.py](tests/e2e/test_mqtt_e2e.py). Runtime code and test files are unchanged by this metadata update.

Metadata validation: 116 checks passed across the 29 repositories (canonical license text, new documentation links and headers, source citations, and metadata-only change scope).
