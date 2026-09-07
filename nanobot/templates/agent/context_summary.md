Return a strict JSON working-memory checkpoint for the accepted conversation prefix.

The JSON object must contain exactly these fields:

- `schema_version`: the integer `1`
- `tasks`: array of strings
- `constraints`: array of strings
- `explicit_preferences`: array of strings copied exactly from user messages when applicable
- `decisions`: array of strings
- `file_changes`: array of strings
- `errors`: array of strings
- `evidence_refs`: array of exact 64-character artifact IDs already present in the conversation
- `remaining_work`: array of strings

Use empty arrays when a category has no grounded entry. Do not invent preferences, artifact IDs,
completed changes, or successful tool results. Preserve exact identifiers, paths, commands,
requirements, unresolved blockers, and next actions needed to continue the task. Return only the
JSON object without a code fence or commentary.
