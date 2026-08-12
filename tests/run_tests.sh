#!/usr/bin/env bash
# Assert the parser's behaviour on tests/fixtures/nesting.wdl, which is built to contain
# every construct that has actually caused trouble: decoy `call`/`if`/`scatter` text inside
# both command-block styles, a scatter nested in an if, a multi-line scatter expression, a
# multi-line call input block, a dependency threaded through an intermediate declaration,
# and a call into an imported file.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tool="${here}/../wdl_flowchart.py"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fail=0
check() {
    local what="$1" expected="$2" got="$3"
    if [[ "$expected" == "$got" ]]; then
        printf '  ok    %s\n' "$what"
    else
        printf '  FAIL  %s: expected %q, got %q\n' "$what" "$expected" "$got"
        fail=1
    fi
}
has() {
    local what="$1" pattern="$2"
    if grep -qE "$pattern" "$dot"; then
        printf '  ok    %s\n' "$what"
    else
        printf '  FAIL  %s: no match for %s\n' "$what" "$pattern"
        fail=1
    fi
}
hasnt() {
    local what="$1" pattern="$2"
    if grep -qE "$pattern" "$dot"; then
        printf '  FAIL  %s: unexpected match for %s\n' "$what" "$pattern"
        fail=1
    else
        printf '  ok    %s\n' "$what"
    fi
}

echo "== nesting.wdl"
summary="$(python3 "$tool" "${here}/fixtures/nesting.wdl" -o "$tmp")"
dot="${tmp}/nesting.flow.dot"

# Counted from the tool's own summary line, not by grepping the .dot: a label grep also
# matches the INPUTS/OUTPUTS note boxes and the legend.
check "call count"  "5" "$(sed -nE 's/.*\(([0-9]+) calls.*/\1/p' <<<"$summary")"
check "edge count"  "3" "$(sed -nE 's/.*, ([0-9]+) edges.*/\1/p' <<<"$summary")"
check "no unresolved refs" "0" "$(grep -c 'unresolved' <<<"$summary" | tr -d ' ')"

has    "scatter nested inside if"        'cluster_1.*\n?'
has    "if cluster labelled"             'label="if \(defined\(label\)\)"'
has    "multi-line scatter joined"       'label="scatter \(pair in zip\( bams, bams\)\)"'
has    "imported call marked"            'imported \[label="imported.*diagonals'
has    "dep through a declaration"       '^  perShard -> gather$'
has    "dep into imported call"          '^  prepare -> imported$'
hasnt  "decoy in command <<< >>>"        'notARealCall|\bfake\b'
hasnt  "decoy in command \{ \}"          'alsoFake'

# nesting order: the scatter cluster must open after the if cluster and before its close
if python3 - "$dot" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
i_if = s.index('label="if (defined(label))"')
i_sc = s.index('label="scatter (b in bams)"')
sys.exit(0 if i_if < i_sc else 1)
PY
then printf '  ok    scatter cluster emitted inside the if cluster\n'
else printf '  FAIL  scatter cluster is not inside the if cluster\n'; fail=1
fi

echo "== --check staleness"
if python3 "$tool" "${here}/fixtures/nesting.wdl" -o "$tmp" --check >/dev/null; then
    printf '  ok    fresh .dot passes --check\n'
else
    printf '  FAIL  fresh .dot failed --check\n'; fail=1
fi
echo "// tampered" >> "$dot"
if python3 "$tool" "${here}/fixtures/nesting.wdl" -o "$tmp" --check >/dev/null 2>&1; then
    printf '  FAIL  --check passed a stale .dot\n'; fail=1
else
    printf '  ok    stale .dot fails --check\n'
fi

echo
if [[ "$fail" -eq 0 ]]; then echo "ALL TESTS PASSED"; else echo "TESTS FAILED"; fi
exit "$fail"
