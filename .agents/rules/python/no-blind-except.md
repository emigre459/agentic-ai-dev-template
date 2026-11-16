---
description: Catch the narrowest exception types; a catch-all (bare except, Exception, BaseException) only when it re-raises, or when absolutely necessary and agreed with the human dev.
appliesTo: "**/*.py"
---

# No blind excepts

Catch the narrowest exception types that the guarded call can raise. A catch-all
handler (`except:`, `except Exception`, `except BaseException`, or a tuple that
contains either) hides bugs: a typo or a programming error is handled like the
expected failure.

A catch-all is allowed only when:

1. its body re-raises at top level (`raise`, or `raise X from exc`), or
2. it is absolutely necessary AND a human dev agreed. Mark the `except` header (any line of it):

   ```python
   except Exception:  # noqa: BLE001 -- plugin boundary must not crash the run; agreed <who> #<PR>
   ```

   The marker must say why, then end the line with `; agreed <who> #<PR>`. Ask the
   human; do not write `agreed` on your own authority.

Logging the exception does NOT make a catch-all acceptable. Also see
`../shared/fail-hard-not-warn.md` for what a handler should do with what it catches.

## Ruff versus the rule

The rule is stricter than ruff `BLE001`:

- ruff exempts handlers that log the exception; the rule does not.
- `# noqa: BLE001` without the `; agreed <who> #<PR>` ending silences ruff but still
  breaks the rule.
- A conditional or nested re-raise may satisfy ruff; the rule needs a `raise` at the
  top level of the handler body.

## Enforcement

- Ruff `BLE001` (`BLE` in the ruff `select` in `stacks/python/pyproject.toml`) catches
  the unlogged catch-alls at lint time.
- The stricter parts (logged handlers, the agreed marker) are reviewer-enforced.
