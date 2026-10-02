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
# report names the dependency that broke it.
#
# On success the updated flake.lock is left in place for the caller to
# commit; on a regression the original flake.lock is restored.
#
# Usage: flake-check [--flake DIR] [--report FILE] [INPUT...]
#   INPUT...       inputs to update (default: all of them)
#   --flake DIR    the flake to check (default: .)
#   --report FILE  write a markdown summary there (for a PR or issue body)
# Env: JOBS (parallel evals, default 4), NIX_FLAGS (extra nix flags)
# Exit: 0 updated, no regressions · 1 regression (lock restored)
#       2 error / no verdict      · 3 nothing to update (already at tip)

set -euo pipefail

FLAKE="."; REPORT=""; INPUTS=()
while (( $# )); do
    case "$1" in
        --flake) FLAKE="${2:?--flake needs a directory}"; shift ;;
        --report) REPORT="${2:?--report needs a file}"; shift ;;
        -h|--help) sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

# "<input> <rev-or-narHash>" for every root input that owns a lock node
# (inputs that `follows` another are arrays and move with their target).
revs() {
    jq -r '. as $l | $l.nodes[$l.root].inputs // {} | to_entries[]
        | select(.value | type == "string")
        | "\(.key) \($l.nodes[.value].locked | .rev // .narHash // "?")"' "$1" | sort
}

# Every attr worth evaluating, one per line.
list_attrs() {
    nix eval --json "${NIXF[@]}" --impure --expr "
        let o = (builtins.getFlake \"${FLAKE}\").outputs; in
        map (h: \"nixosConfigurations.\\\"\${h}\\\".config.system.build.toplevel\")
            (builtins.attrNames (o.nixosConfigurations or {}))
        ++ map (c: \"checks.${SYSTEM}.\\\"\${c}\\\"\")
            (builtins.attrNames ((o.checks or {}).${SYSTEM} or {}))" | jq -r '.[]'
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
# Parallel evals of the same git inputs race in nix's fetcher cache
# ("getting Git object …: object not found"), so each failure gets one
# serial retry before it counts.
phase() {
    local dir="${WORK}/$1" n=0 a
    mkdir -p "${dir}"
    while read -r a; do
        n=$((n + 1)); throttle; eval_one "${a}" "${dir}/${n}" &
    done <<< "$2"
    wait
    n=0
    while read -r a; do
        n=$((n + 1))
        [[ "$(cat "${dir}/${n}.res")" == ok ]] || eval_one "${a}" "${dir}/${n}"
    done <<< "$2"
}

short() { local s="${1#nixosConfigurations.}"; s="${s%.config.system.build.toplevel}"; echo "${s//\"/}"; }

ATTRS="$(list_attrs)" || { echo "error: could not evaluate the flake's outputs" >&2; exit 2; }
[[ -n "${ATTRS}" ]] || { echo "error: no nixosConfigurations or ${SYSTEM} checks to evaluate" >&2; exit 2; }
COUNT="$(wc -l <<< "${ATTRS}")"
echo "${COUNT} attrs; baseline eval…"
phase base "${ATTRS}"

echo "updating: ${INPUTS[*]:-all inputs}"
nix flake update "${NIXF[@]}" --flake "${FLAKE}" "${INPUTS[@]}"
cp "${LOCK}" "${WORK}/flake.lock.new"
# "<input> <from> <to>" for every input the update moved.
join -j1 <(revs "${WORK}/flake.lock.orig") <(revs "${LOCK}") | awk '$2 != $3' > "${WORK}/changed"
if [[ ! -s "${WORK}/changed" ]]; then
    echo "nothing to update: ${INPUTS[*]:-every input} already at the tip of what it follows"
    exit 3
fi
echo "candidate eval…"
phase cand "${ATTRS}"

# --- verdict
REG=(); REGATTRS=""; FIXED=(); OLD=(); n=0
while read -r a; do
    n=$((n + 1))
    case "$(cat "${WORK}/base/${n}.res")/$(cat "${WORK}/cand/${n}.res")" in
        ok/fail) REG+=("$(short "${a}")|${WORK}/cand/${n}.log"); REGATTRS+="${a}"$'\n' ;;
        fail/ok) FIXED+=("$(short "${a}")") ;;
        fail/fail) OLD+=("$(short "${a}")") ;;
    esac
done <<< "${ATTRS}"
REGATTRS="${REGATTRS%$'\n'}"

# Blame: with more than one input moved, update each one alone on top of
# the original lock and re-evaluate only the regressed attrs.
declare -A BLAME=()
if (( ${#REG[@]} )) && (( $(wc -l < "${WORK}/changed") > 1 )); then
    echo "attributing ${#REG[@]} regression(s) to inputs…"
    while read -r i _; do
        cp "${WORK}/flake.lock.orig" "${LOCK}"
        nix flake update "${NIXF[@]}" --flake "${FLAKE}" "${i}" </dev/null >/dev/null 2>&1 || continue
        phase "blame-${i}" "${REGATTRS}"
        m=0
        while read -r a; do
            m=$((m + 1))
            [[ "$(cat "${WORK}/blame-${i}/${m}.res")" == fail ]] && BLAME["$(short "${a}")"]+="${i} "
        done <<< "${REGATTRS}"
    done < "${WORK}/changed"
    cp "${WORK}/flake.lock.new" "${LOCK}"
fi

summary() {
    echo "### flake-check: ${INPUTS[*]:-all inputs}"
    echo
    echo "| input | from | to |"
    echo "|---|---|---|"
    while read -r i from to; do echo "| \`${i}\` | \`${from:0:12}\` | \`${to:0:12}\` |"; done < "${WORK}/changed"
    echo
    echo "Evaluated ${COUNT} attrs (nixosConfigurations toplevels + \`checks.${SYSTEM}\`) before and after the update."
    echo
    if (( ${#REG[@]} )); then
        echo "**${#REG[@]} regression(s)** — evaluated before the update, fail after:"
        echo
        local r name log who
        for r in "${REG[@]}"; do
            name="${r%%|*}"; log="${r#*|}"
            who="${BLAME[${name}]:-}"
            if (( $(wc -l < "${WORK}/changed") == 1 )); then who="$(cut -d' ' -f1 "${WORK}/changed")"; fi
            echo "<details><summary><code>${name}</code> — broken by: ${who:-only the combination (no single input alone)}</summary>"
            echo
            echo '```'
            grep -v '^warning\|evaluation warning' "${log}" | tail -25
            echo '```'
            echo "</details>"
        done
    else
        echo "**No regressions.**"
    fi
    if (( ${#FIXED[@]} )); then echo; echo "Fixed by the update: \`${FIXED[*]}\`"; fi
    if (( ${#OLD[@]} )); then echo; echo "Already failing before the update (not counted): \`${OLD[*]}\`"; fi
}

summary | tee "${REPORT:-/dev/null}"
if (( ${#REG[@]} )); then
    cp "${WORK}/flake.lock.orig" "${LOCK}"
    echo "flake.lock restored." >&2
    exit 1
fi
