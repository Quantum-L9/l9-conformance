<!-- L9_PROTECTED_ROOT_PR -->
<!--
  The stamp above is required in the PR body when any additive_only root file
  is in the diff (ops/config/root-file-protection.json). make pr fills this
  Protected-root block. When no additive_only path is touched, the composer
  marks the section N/A.

  Prefer append-only edits. A rewrite/deletion also needs a commit line:
    ALLOW-ROOT-DELETION: <path> — <reason with proof of necessity>
-->

## Protected-root

### Paths

<!-- One path per line. Must match the additive_only diff. Composer fills. -->

- ` `

### Edit mode (pick one per path)

- [ ] **Append-only** — existing lines kept; only new lines added (no `ALLOW-ROOT-DELETION`)
- [ ] **Justified rewrite** — commit contains `ALLOW-ROOT-DELETION: <path> — <reason>`

### Why a root file

<!-- What cannot be done in a non-root path. Composer fills. -->

### Proof of necessity (rewrites only)

<!-- Issue, failing gate, or law citation. Empty if every path is append-only. -->

## Problem

<!-- REQUIRED. The error, bug, or gap this fixes. Lead with the symptom a human saw.
     Paste the actual traceback, failing assertion, alert, or log line. -->

```
paste the error / failing output here, or delete this block and describe the gap
```

Closes #

## Type of Change

- [ ] Bug fix
- [ ] Feature / enhancement
- [ ] Refactor (no behavior change)
- [ ] Documentation
- [ ] CI / governance change
- [ ] Breaking change (see rollback below)

## Fix

<!-- What you changed to make the problem above go away. Note alternatives you rejected and why. -->

## Risk

<!-- Pick exactly one. This routes how hard reviewers look. -->

- [ ] Low — additive, reversible, no data or contract change
- [ ] Medium — touches shared code, config, or a public interface
- [ ] High — breaking change, migration, IAM/network, or irreversible

Blast radius:
Rollback:

## Evidence

<!-- Show the problem is gone. Pasted output or a CI link. Not "tests pass". -->

```
$ pytest -q
$ ruff check . && pyright
```

## Gates

<!-- Leave unchecked. make pr checks a box only when a measurement exists
     and marks every other box not measured. -->

- [ ] Regression test added that fails without this fix
- [ ] No secrets, tokens, or customer data in code, tests, fixtures, or logs
- [ ] `semgrep` clean, or findings triaged below
- [ ] New IAM / workflow permissions are least privilege and enumerated
- [ ] Third-party actions pinned to a full commit SHA
- [ ] Public interface change is documented and versioned
- [ ] Observability exists for the new path (metric, log, trace, or alert)

## Semantic change

<!-- Applies when the diff touches semantics/ or docs/semantic-foundation/. Otherwise tick N/A. -->

- [ ] N/A — no canonical semantic artifact or release record changed
- Foundation / release candidate affected:
- Prior exact coordinate or source identity (artifact_id, digest, contract/binding id):
- New exact coordinate (when applicable):
- [ ] Transitive semantic dependents revalidated (`semantics/semantic_dependency_model.yaml`)
- [ ] `scripts/validate-semantics.py` result pasted under Evidence
- [ ] `docs/semantic-foundation/<version>/` manifest, `HASHES.sha256`, and `RELEASE_VALIDATION.md` updated when canonical bytes changed
- [ ] Admission / successor requirement checked (`L9-REVISION-001`: an admitted revision is immutable; material change needs a successor)

## Reviewer focus

<!-- Where to look hardest. Trade-offs accepted. Deferred follow-ups, with issue links. -->

## Changes by intent

<!-- Composer fills one `path — why` line per name-status row. -->

**Added**
- `path/to/new_file.py` — why this file needs to exist

**Modified**
- `path/to/existing.py` — what changed in it and why

**Deleted**
- `path/to/dead.py` — why it is safe to remove

## Files touched

<!-- Auto-filled by make pr / .github/workflows/pr-files.yml. Do not edit by hand. -->

<!-- FILES-TOUCHED:START -->
_pending — the bot fills this in on push_
<!-- FILES-TOUCHED:END -->
