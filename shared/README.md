# shared/

Platform modules this service imports, committed so a plain clone runs and tests
without the platform repo. Only what `app/` reaches through its imports is here (49
files); `python -m app.shared_check` fails if one is missing.

Copied from the platform repo's `shared/` on 2026-11-15 (its `develop` at
`8f102efc`), then pruned to the import closure of `app/`. The plan is for this copy
to become the source and for the platform repo to use it from here; until that
happens, a change made in one copy must be made in the other.
