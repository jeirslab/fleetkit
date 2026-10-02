#!/usr/bin/env bash
#
# flake-check — would updating this flake's inputs break it?
#
# Evaluates every nixosConfiguration's toplevel and every checks.<system>
# derivation (drvPath only, nothing is built), runs `nix flake update` —
# every input, or just the ones named, each moving to the tip of whatever its
# flake.nix reference follows — then evaluates the same set again. Only an
# attr that evaluated before the update and fails after it is a regression.
# For each regression it then tries every changed input on its own, so the
# result names the dependency that broke it.
#
# Evaluation is bulk first: one nix process, every attr under
# builtins.tryEval, so nixpkgs and shared modules are evaluated once. What
# tryEval can't catch (type errors, abort, infinite recursion) or a bulk run
# that dies outright falls back to one `nix eval` per attr, which is also
# where each failure's real error message comes from.
#
# On success the updated flake.lock is left in place for the caller to
# commit; on a regression the original flake.lock is restored.
#
# Usage: flake-check [--flake DIR] [--json FILE] [--report FILE] [--no-bulk] [INPUT...]
#   INPUT...       inputs to update (default: all of them)
#   --flake DIR    the flake to check (default: .)
#   --json FILE    write the result as JSON (the source of truth)
#   --report FILE  write a markdown summary rendered from that JSON
#   --no-bulk      skip the one-process pass (huge flakes, tight memory)
# Env: JOBS (parallel per-attr evals, default 4), NIX_FLAGS (extra nix flags)
# Exit: 0 updated, no regressions · 1 regression (lock restored)
#       2 error / no verdict      · 3 nothing to update (already at tip)

set -euo pipefail

FLAKE="."; JSON=""; REPORT=""; BULK=1; INPUTS=()
while (( $# )); do
    case "$1" in
        --flake) FLAKE="${2:?--flake needs a directory}"; shift ;;
        --json) JSON="${2:?--json needs a file}"; shift ;;
        --report) REPORT="${2:?--report needs a file}"; shift ;;
        --no-bulk) BULK=0 ;;
        -h|--help) sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "error: unknown flag '$1'" >&2; exit 2 ;;
        *) INPUTS+=("$1") ;;
    esac
    shift
done
FLAKE="$(cd "${FLAKE}" && pwd)"
LOCK="${FLAKE}/flake.lock"
[[ -f "${LOCK}" ]] || { echo "error: ${LOCK} not found" >&2; exit 2; }
for i in "${INPUTS[@]}"; do
    jq -e --arg i "${i}" '.nodes[.root].inputs[$i]' "${LOCK}" >/dev/null \
        || { echo "error: '${i}' is not a root input of ${LOCK}" >&2; exit 2; }
done

JOBS="${JOBS:-4}"
read -r -a NIXF <<< "${NIX_FLAGS:-}"
SYSTEM="$(nix eval --raw --impure --expr builtins.currentSystem)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
cp "${LOCK}" "${WORK}/flake.lock.orig"

# {input: rev-or-narHash} for every root input that owns a lock node (inputs
# that `follows` another are arrays and move with their target).
revs() {
    jq -c '. as $l | $l.nodes[$l.root].inputs // {} | to_entries
        | map(select(.value | type == "string")
              | {key, value: ($l.nodes[.value].locked | .rev // .narHash // "?")})
        | from_entries' "$1"
}

# The attrs worth evaluating, as CLI attr paths, one per line.
list_attrs() {
    nix eval --json "${NIXF[@]}" --impure --expr "
        let o = (builtins.getFlake \"${FLAKE}\").outputs; in
        map (h: \"nixosConfigurations.\\\"\${h}\\\".config.system.build.toplevel\")
            (builtins.attrNames (o.nixosConfigurations or {}))
        ++ map (c: \"checks.${SYSTEM}.\\\"\${c}\\\"\")
            (builtins.attrNames ((o.checks or {}).${SYSTEM} or {}))" </dev/null | jq -r '.[]'
}

# One process, every attr under tryEval: {"<attr path>": true|false}.
# false means "tryEval caught an error"; an uncatchable error kills the run.
bulk_eval() {  # <out.json>
    nix eval --json "${NIXF[@]}" --impure --expr "
        let
          o = (builtins.getFlake \"${FLAKE}\").outputs;
          ok = d: (builtins.tryEval (builtins.seq d.drvPath true)).success;
        in
          builtins.listToAttrs (
            map (h: { name = \"nixosConfigurations.\\\"\${h}\\\".config.system.build.toplevel\";
                      value = ok o.nixosConfigurations.\${h}.config.system.build.toplevel; })
                (builtins.attrNames (o.nixosConfigurations or {}))
            ++ map (c: { name = \"checks.${SYSTEM}.\\\"\${c}\\\"\";
                         value = ok o.checks.${SYSTEM}.\${c}; })
                (builtins.attrNames ((o.checks or {}).${SYSTEM} or {})))" \
        </dev/null >"$1" 2>"$1.err"
}

eval_one() {  # <attr> <out-prefix>
    if nix eval --raw "${NIXF[@]}" "${FLAKE}#$1.drvPath" </dev/null >/dev/null 2>"$2.log"; then
        echo ok > "$2.res"; rm -f "$2.log"
    else
        echo fail > "$2.res"
    fi
}

throttle() { while (( $(jobs -rp | wc -l) >= JOBS )); do wait -n || true; done; }

# Evaluate the attrs in $2 (newline list) into ${WORK}/<phase>/<n>.{res,log}.
# $3 = 1 to try the bulk pass first. Per-attr evals run in parallel, and
# each failure gets one serial retry: parallel evals of the same git inputs
# race in nix's fetcher cache ("getting Git object …: object not found").
phase() {
    local dir="${WORK}/$1" bulk="${3:-0}" n=0 a
    mkdir -p "${dir}"
    if (( bulk )) && bulk_eval "${dir}/bulk.json"; then
        while read -r a; do
            n=$((n + 1))
            [[ "$(jq -r --arg a "${a}" '.[$a]' "${dir}/bulk.json")" == true ]] && echo ok > "${dir}/${n}.res"
        done <<< "$2"
    elif (( bulk )); then
        echo "  bulk pass died ($(grep -m1 'error:' "${dir}/bulk.json.err" | cut -c1-120)); per-attr" >&2
    fi
    n=0
    while read -r a; do
        n=$((n + 1))
        [[ -f "${dir}/${n}.res" ]] && continue
        throttle; eval_one "${a}" "${dir}/${n}" &
    done <<< "$2"
    wait
    n=0
    while read -r a; do
        n=$((n + 1))
        [[ "$(cat "${dir}/${n}.res")" == ok ]] || eval_one "${a}" "${dir}/${n}"
    done <<< "$2"
}

short() { local s="${1#nixosConfigurations.}"; s="${s%.config.system.build.toplevel}"; echo "${s//\"/}"; }

# Last lines of an eval's stderr, minus nix's warning chatter.
errtail() { grep -v '^warning\|evaluation warning' "$1" 2>/dev/null | tail -25; }

ATTRS="$(list_attrs)" || { echo "error: could not evaluate the flake's outputs" >&2; exit 2; }
[[ -n "${ATTRS}" ]] || { echo "error: no nixosConfigurations or ${SYSTEM} checks to evaluate" >&2; exit 2; }
COUNT="$(wc -l <<< "${ATTRS}")"
echo "${COUNT} attrs; baseline eval…" >&2
phase base "${ATTRS}" "${BULK}"

echo "updating: ${INPUTS[*]:-all inputs}" >&2
nix flake update "${NIXF[@]}" --flake "${FLAKE}" "${INPUTS[@]}" </dev/null >&2
cp "${LOCK}" "${WORK}/flake.lock.new"
# [{input, from, to}] for every input the update moved.
jq -n --argjson a "$(revs "${WORK}/flake.lock.orig")" --argjson b "$(revs "${LOCK}")" \
    '[$b | to_entries[] | select($a[.key] != .value) | {input: .key, from: $a[.key], to: .value}]' \
    > "${WORK}/changed.json"

emit() {  # <status> — write --json and --report from what's known so far
    local status="$1"
    [[ -f "${WORK}/results.json" ]] || echo '[]' > "${WORK}/results.json"
    # --slurpfile, not --argjson: error tails can outgrow one argv string.
    jq -n --arg status "${status}" --arg system "${SYSTEM}" --argjson count "${COUNT}" \
        --slurpfile changed "${WORK}/changed.json" --slurpfile results "${WORK}/results.json" '
        $results[0] as $r |
        { status: $status, system: $system, evaluated: $count, changed: $changed[0],
          regressions: [$r[] | select(.before == "ok" and .after == "fail")],
          fixed: [$r[] | select(.before == "fail" and .after == "ok") | .attr],
          already_failing: [$r[] | select(.before == "fail" and .after == "fail") | .attr] }' \
        > "${WORK}/result.json"
    [[ -z "${JSON}" ]] || cp "${WORK}/result.json" "${JSON}"
    render < "${WORK}/result.json" | tee "${REPORT:-/dev/null}"
}

render() {  # markdown from the result JSON on stdin
    jq -r '
      def code: "`" + . + "`";
      "### flake-check: " + (if (.changed | length) == 0 then "nothing to update"
                             else (.changed | map(.input) | join(", ")) end),
      "",
      (if (.changed | length) > 0 then
        "| input | from | to |", "|---|---|---|",
        (.changed[] | "| \(.input | code) | \(.from[0:12] | code) | \(.to[0:12] | code) |"), ""
       else empty end),
      "Evaluated \(.evaluated) attrs (nixosConfigurations toplevels + `checks.\(.system)`) before and after the update.",
      "",
      (if (.regressions | length) > 0 then
        "**\(.regressions | length) regression(s)**: evaluated before the update, fail after:", "",
        (.regressions[] |
          "<details><summary>\(.attr | code), broken by: \(if (.blame | length) > 0 then (.blame | join(", ")) else "only the combination (no single input alone)" end)</summary>",
          "", "```", .error, "```", "</details>")
       elif .status == "nothing" then "Nothing to update."
       else "**No regressions.**" end),
      (if (.fixed | length) > 0 then "", "Fixed by the update: \(.fixed | map(code) | join(" "))" else empty end),
      (if (.already_failing | length) > 0 then "", "Already failing before the update (not counted): \(.already_failing | map(code) | join(" "))" else empty end)'
}

if [[ "$(jq length "${WORK}/changed.json")" == 0 ]]; then
    echo "nothing to update: ${INPUTS[*]:-every input} already at the tip of what it follows" >&2
    emit nothing >/dev/null
    exit 3
fi

echo "candidate eval…" >&2
phase cand "${ATTRS}" "${BULK}"

# --- per-attr results
n=0; REGATTRS=""
: > "${WORK}/results.ndjson"
while read -r a; do
    n=$((n + 1))
    b="$(cat "${WORK}/base/${n}.res")"; c="$(cat "${WORK}/cand/${n}.res")"
    err=""; [[ "${b}/${c}" == ok/fail ]] && { err="$(errtail "${WORK}/cand/${n}.log")"; REGATTRS+="${a}"$'\n'; }
    [[ "${b}/${c}" == fail/fail ]] && err="$(errtail "${WORK}/base/${n}.log")"
    jq -nc --arg attr "$(short "${a}")" --arg path "${a}" --arg before "${b}" --arg after "${c}" --arg error "${err}" \
        '{attr: $attr, path: $path, before: $before, after: $after, error: $error, blame: []}' >> "${WORK}/results.ndjson"
done <<< "${ATTRS}"
REGATTRS="${REGATTRS%$'\n'}"

# Blame: update each changed input alone on top of the original lock and
# re-evaluate only the regressed attrs. With one changed input it's that one.
if [[ -n "${REGATTRS}" ]]; then
    mapfile -t CHANGED < <(jq -r '.[].input' "${WORK}/changed.json")
    if (( ${#CHANGED[@]} == 1 )); then
        jq -c --arg i "${CHANGED[0]}" 'if .before == "ok" and .after == "fail" then .blame = [$i] else . end' \
            "${WORK}/results.ndjson" > "${WORK}/r.tmp" && mv "${WORK}/r.tmp" "${WORK}/results.ndjson"
    else
        echo "attributing regressions to ${#CHANGED[@]} changed inputs…" >&2
        for i in "${CHANGED[@]}"; do
            cp "${WORK}/flake.lock.orig" "${LOCK}"
            nix flake update "${NIXF[@]}" --flake "${FLAKE}" "${i}" </dev/null >/dev/null 2>&1 || continue
            phase "blame-${i}" "${REGATTRS}" 0
            m=0
            while read -r a; do
                m=$((m + 1))
                [[ "$(cat "${WORK}/blame-${i}/${m}.res")" == fail ]] || continue
                jq -c --arg p "${a}" --arg i "${i}" 'if .path == $p then .blame += [$i] else . end' \
                    "${WORK}/results.ndjson" > "${WORK}/r.tmp" && mv "${WORK}/r.tmp" "${WORK}/results.ndjson"
            done <<< "${REGATTRS}"
        done
        cp "${WORK}/flake.lock.new" "${LOCK}"
    fi
fi
jq -s . "${WORK}/results.ndjson" > "${WORK}/results.json"

if [[ -n "${REGATTRS}" ]]; then
    emit regression
    cp "${WORK}/flake.lock.orig" "${LOCK}"
    echo "flake.lock restored." >&2
    exit 1
fi
emit ok
