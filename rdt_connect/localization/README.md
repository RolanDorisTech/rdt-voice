# RDT Connect native localization — English remediation v10

**Status: candidate for final independent review; translation remains blocked until the v10 audit passes.** This is a source/catalog remediation package, not the complete runtime localization integration.

## Scope and changes
- Native RDT Connect source only. No edits to `~/dev/rdt-voice` or embedded browser locale files.
- English catalog has 881 messages: 759 `TRANSLATE`, 122 `KEEP_ENGLISH`.
- v10 package carries forward the previously audited repairs the remaining audited pseudo-plural/placeholder issues in stop-flow messages; uses semantic Rust format placeholders; and classifies the backend-candidate dump records as technical diagnostics that stay in English.
- Remaining translated messages with positional `{argN}` placeholders have definitions in the inventory `placeholder_notes` field.
- Catalog version is 4. Existing surviving message keys are retained by `source_to_key_map.json`; do not regenerate keys from English text.
- Earlier v5 repairs to rich help messages, CA confirmation plurals, cache-cleanup wording, implementation-token exclusions, and obsolete language-selector strings remain included.

## Important limitation
This package does **not** yet enable the language selector or install translated locale files. It repairs the English source/catalog contract. Runtime integration and the actual Mac build remain separate verification steps.

## Validation
Run `python3 localization/validate_locales.py src/locales --english-only` from the project root after application. It verifies valid JSON, inventory/key parity, classification counts, and semantic notes for translated positional placeholders. Installer dry-run must pass first; this package is intentionally restricted to the exact source hashes listed in `install.py`.

## Translator policy
- `TRANSLATE`: translate the full message naturally, preserving every placeholder exactly.
- `KEEP_ENGLISH`: copy the English string byte-for-byte into each locale. Raw diagnostics are intentionally English.
- No implementation-only keys or incomplete fragments may be reintroduced.
- Preserve safe rich-message slot placeholders literally; do not add HTML or markup.

## Frozen artifact hashes
- English catalog SHA-256: `ee4833815b195b3e83450bac29ae0cd0ada9457219f2cc878d729466fc36ef42`
- Inventory SHA-256: `8ddb7729770d02e21405c5e6eacb695e20ca86aa3746627020937289e52da8d0`
- Source-key map SHA-256: `a7ef8e75ec4c9599f80663a04ff7d0b3e304c9f3c9ebc7a48f3d83a4d909ec6d`

## What has and has not been tested
- Passed: catalog/inventory validation; guarded installer dry-run on a disposable project created from the exact baseline; install and rollback on disposable files; rejection of a path-traversal manifest; refusal to rollback over a changed target; ZIP and embedded checksum validation.
- Not run: Rust/Tauri compilation or native macOS runtime. These remain required gates after the guarded patch is dry-run and applied on the user's Mac.


## V10 audit fixes

The four audited raw process-snapshot, manager-process, and port-listener diagnostics now use semantically named placeholders in Rust source and the English catalog while remaining KEEP_ENGLISH. The locale validator verifies exact required placeholder sets and per-placeholder notes for those messages. Installer and rollback reject symlinked ancestor components even when a symlink resolves to another directory within the app root; paths are checked again before mutation.


## V10 validation hardening

The validator compares each inventory row's `placeholders` field against the exact sorted, distinct placeholder names extracted from that row's English catalog value. Any missing, extra, duplicate, or misordered inventory placeholder token fails validation. Installer and rollback both inspect every lexical absolute-path component (including ancestors above the project root) for symlinks before trusting the path; the code does not resolve first and thereby hide symlink ancestors. Regression tests cover incorrect inventory placeholder fields, project-root/testing symlinks, clean rollback, modified installed files, and path traversal.


## V10 safeguards

The validator and installer compare every inventory `placeholders` field with the exact sorted set of named placeholders in the English string. Missing, extra, duplicate, or incorrectly recorded placeholder names fail validation. Installer and rollback reject symlinks in any lexical absolute path component, including components above the project root, before calling path resolution. Regression tests intentionally corrupt an inventory placeholder field and introduce a `project/testing` symlink to confirm both cases fail safely.


## V9 metadata consistency correction

The extraction report sourceMapSha256 and this README’s frozen artifact hashes were reconciled against the actual packaged files. The locale validator and guarded installer now verify the English catalog, inventory, and source-key-map hashes declared in extraction_report.json; a stale or corrupted report hash fails validation before installation.

Key-migration report SHA-256: `7ae795613f59b2ec4b4f7a70ee3dcba252ad0f7b01c4917b3c20c3cf72c956fe`.
