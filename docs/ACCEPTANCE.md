# V1 Acceptance Contract

Jobops separates sanitized-fixture acceptance from live-site reliability. CI and local tests never create accounts or submit real applications.

## Fixture metrics

`tests/test_acceptance_metrics.py` runs four supported ATS adapters against fictitious application forms. Its Workday case traverses one sanitized deterministic journey through `autofillWithResume` → `myInformation` → `myExperience` → `applicationQuestions` → `voluntaryDisclosures` → `selfIdentify` → `review`, including an exact byte read-back of a synthetic resume upload and exact verified synthetic answers. This fixture contract requires:

- Review arrival: 5/5 (100%), above the 95% V1 threshold;
- median model calls: 0, with every supported adapter explicitly reporting zero rather than relying on missing telemetry.

The same reusable Workday fixture driver is exercised separately by `test_sanitized_workday_fixture_reaches_review_through_multiple_stages`. Both contracts require exact read-back bindings, a non-resumed journey, the expected Review checkpoint, and every deterministic stage transition. They do not exercise a live tenant, login, registration, email verification, or changing production markup. Direct-Review fixtures remain limited to resumed-Review and Gate B safety contracts and are not counted as Review arrival.

The wider contracts additionally require:

- unknown required questions remain empty and return a handoff;
- sensitive unknowns use a dedicated handoff status;
- candidate values never appear in Review outcomes;
- a duplicate application cannot cause a second submit click;
- a click without confirmation becomes `SUBMIT_UNKNOWN` and is never retried;
- every `SUBMITTED_VERIFIED` carries eligible evidence;
- payloads and secrets do not enter Node process arguments;
- Keychain operations use Security.framework directly.

Run:

```bash
.venv/bin/python -m pytest -q --ignore=tests/test_real_forms.py
```

The acceptance test does not skip when Chromium is missing. CI installs Chromium explicitly, and a launch failure fails the release gate.

## Live metrics

The multi-stage fixture result is not a production claim and must not be combined into a live reliability percentage. Live Review arrival must be measured separately by ATS, adapter version, and date. A live run may never be added to default CI, and it must not submit without the normal permits.
