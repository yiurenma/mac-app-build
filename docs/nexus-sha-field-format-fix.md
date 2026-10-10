# Nexus SHA extraction fix

Based on repository commit db1d5ab (v0.7.6) and the supplied Jenkins screenshot.

## Confirmed defect

The target tarball filename contains version `1.3.202610101332-SNAPSHOT.2`, while its JSON `version` is `1.3.202610101332`. The previous parser rejects that valid pair. Exact target filename matching now remains authoritative, and metadata may contain either the full version or the base with the terminal `-SNAPSHOT.<number>` removed. Other version mismatches, checksum conflicts, missing targets and invalid hashes remain errors. Sidecar files remain excluded.

## Marker compatibility and remaining uncertainty

The diagnostic screenshot reports no Nexus JSON group found, before the version check. The previous normalizer handles time-only prefixes but not ISO date/time prefixes. Added ISO timestamp handling to both group and JSON lines, also used by final-result detection. An unrecognized group now reports the number of title-containing lines rejected, without dumping raw log contents.

The raw console response was not available: ISO prefixes are a tested compatibility fix, not a confirmed explanation of the exact live marker mismatch. If the same missing-group failure persists, the new rejected-line count distinguishes an absent title from unsupported formatting; the exact raw marker prefix will still need inspection.

## Validation

- 19 focused Nexus tests pass, including seven new regression tests.
- Fixture reconstructs the photographed JSON shape and Journey hash; the DomainModel tarball hash is synthetic and explicitly identified in the test.
- Full suite: 219 tests run; one failure and three setup errors. Unmodified db1d5ab runs 212 tests with the same failure and three setup errors. The environment lacks `/usr/bin/chromium`; failed browser setup causes subsequent Playwright loop errors. The existing failure is an unrelated diagnostic text assertion.
- No live Jenkins run, builds, or business repository writes were performed.

## Simulated Jenkins response integration tests

`tests/test_nexus_simulated_jenkins.py` exercises the production read-only diagnosis and formal SHA reader with a simulated authenticated HTTP response (no live server/browser). Five additional tests cover:

- >10 MB response with timestamped JSON in the middle and no terminal Finished marker, with independent build SUCCESS.
- Exact version and both checksum comparisons, plus saved diagnostic JSON.
- Incorrect expected Journey SHA fails while the actual value remains the extracted screenshot value.
- A different snapshot build cannot pass the expected-version comparison.
- Missing Nexus JSON cannot pass solely because the build status is SUCCESS.
- Formal build SHA reader consumes the same screenshot-shaped metadata.

All 24 focused Nexus tests pass. This verifies extraction/diagnostic integration with simulated input; it does not establish the exact prefix bytes of the inaccessible real console response or verify live SSO/network behavior.
