# filterByFormula cookbook

`--where` covers equality, emptiness, contains and ranges. Reach for
`--formula` when you need `OR`, nesting, date arithmetic, or anything about a
linked/multi-value field. Clauses from `--formula` and `--where` are combined
with `AND`, so you can mix them.

The CLI URL-encodes whatever you pass — never hand-encode a formula.

## Contents

- [Syntax basics](#syntax-basics)
- [Text](#text)
- [Empty and non-empty](#empty-and-non-empty)
- [Numbers](#numbers)
- [Dates](#dates)
- [Selects, links and multi-value fields](#selects-links-and-multi-value-fields)
- [Combining conditions](#combining-conditions)
- [Gotchas](#gotchas)

## Syntax basics

- Field reference: `{Field Name}` — braces required whenever the name has a
  space, and harmless when it doesn't.
- String literal: `'text'` or `"text"`. Pick the quote the shell isn't already
  using; inside a single-quoted shell argument, use `"` in the formula.
- Comparison: `=`, `!=`, `>`, `>=`, `<`, `<=`.
- Logic: `AND(a, b)`, `OR(a, b)`, `NOT(a)`.
- Formulas are evaluated per record and must return something truthy to keep it.

## Text

```
{Title} = "Ship the docs"                    exact, case-SENSITIVE
LOWER({Title}) = "ship the docs"             exact, case-insensitive
FIND("bug", LOWER({Title})) > 0              contains, case-insensitive
LEFT({Code}, 3) = "INV"                      starts with
REGEX_MATCH({Email}, "@acme\\.io$")          regex (RE2 syntax)
```

`FIND(needle, haystack)` returns 0 when absent, so `> 0` is the "contains"
idiom. `SEARCH()` is the same but returns blank instead of 0.

## Empty and non-empty

```
{Assignee} = BLANK()                         empty
NOT({Assignee} = BLANK())                    not empty
```

`BLANK()` is the reliable form. `{Field} = ""` also works for text fields but
misbehaves on numbers and links.

## Numbers

```
{Score} >= 8
AND({Score} > 0, {Score} < 100)
{Amount} * {Quantity} > 1000                 arithmetic is allowed inline
```

## Dates

Date comparison is where formulas most often fail silently. A `dateTime` cell
carries a time, so `{Due} = "2026-04-01"` matches nothing. Use the date
functions:

```
IS_SAME({Due}, "2026-04-01", 'day')          on that day
IS_AFTER({Due}, TODAY())                     in the future
IS_BEFORE({Due}, TODAY())                    overdue (if not done)
AND(IS_AFTER({Due}, "2026-01-01"), IS_BEFORE({Due}, "2026-04-01"))    window
DATETIME_DIFF(TODAY(), {Created}, 'days') <= 7                        last 7 days
IS_SAME({Due}, TODAY(), 'month')             this month
```

`TODAY()` is a date; `NOW()` is a timestamp. Comparing a `dateTime` field to
`TODAY()` works, but `IS_SAME(..., 'day')` is clearer about intent.

`--where "Due<2026-04-01"` already expands to `IS_BEFORE(...)` for you — this
section matters when you're writing `--formula` by hand.

## Selects, links and multi-value fields

A multi-select or linked-record cell is an array, and comparing an array with
`=` doesn't do what you'd hope. Coerce to a string first:

```
FIND("urgent", ARRAYJOIN({Tags})) > 0        has this tag
ARRAYJOIN({Tags}) = "urgent"                 has ONLY this tag
COUNTA({Owner}) = 0                          no linked records
COUNTA({Owner}) > 1                          more than one link
```

For linked records, `{Owner}` renders as the linked rows' **primary field
values**, so `FIND("Dana", ARRAYJOIN({Owner})) > 0` finds rows linked to a
person named Dana — you're matching the display text, not the record id.

Single-selects behave like text: `{Status} = "Todo"`.

## Combining conditions

```
AND({Status} = "Todo", {Priority} = "High")
OR({Owner} = "alice", {Owner} = "bob")
AND(OR({Status} = "Todo", {Status} = "In progress"), IS_BEFORE({Due}, TODAY()))
NOT({Status} = "Done")
```

`AND()` and `OR()` take any number of arguments, so deep nesting is rarely
needed.

## Gotchas

- **Case sensitivity.** `=` on text is case-sensitive. Wrap both sides in
  `LOWER()` when the user's phrasing might not match the stored casing.
- **A formula error returns no rows, not an error.** An unparseable formula or a
  misspelled field name yields an empty result set that looks exactly like "no
  matches". If a filter surprises you by returning nothing, re-check the field
  name in `schema` before believing it.
- **Field names are literal.** `{Due Date}` and `{Due date}` are different
  fields. Copy them out of `schema`.
- **Formulas run over every record in the table**, so they don't reduce the
  request cost — only the returned volume. A view with a saved filter is cheaper
  when one already exists.
- **`}` in a field name** can't be escaped in a `{...}` reference. If a base has
  one, filter through a view instead.
