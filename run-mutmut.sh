#!/usr/bin/env bash
# run-mutmut.sh — mutation testing runner for History Repair.
#
# Adapted from the sibling nibe-smo-mqtt-bridge-local repo's script of the
# same name — this project mirrors that repo's dev tooling conventions, per
# CLAUDE.md. Lives at the repo root alongside pytest.ini.
# Expects the standard repo layout:
#   app/        — all production Python modules (hr_*.py, flat)
#   tests/      — conftest.py + hr_fakes.py + all test_*.py files
#   pytest.ini  — testpaths=tests, pythonpath=app tests
#
# SOURCE ROOT WORKAROUND
# Mutmut 3.x only recognises '.', 'src/', 'source/' as source roots.
# Our source lives in app/ (on sys.path via pytest.ini pythonpath=app tests).
# This script copies the target module to the repo root before running
# mutmut. The copy is kept after the run so that mutmut show/results work
# (mutmut show needs the source file to apply patches against). Always edit
# app/<module>.py — the root-level copy is regenerated on every run, and is
# deleted at the end of a full ("all") run so it can't shadow app/ during a
# later plain `pytest` invocation (the override below sets pythonpath to
# '. app tests', so a stale root-level copy would silently win over app/'s
# real file).
#
# SCOPE — pure/near-pure modules only, deliberately
# This project's other modules (hr_mariadb.py, hr_sqlite.py, hr_postgres.py,
# hr_web.py, hr_ha_api.py) are exercised by real-database or real-HA
# integration suites, which mutmut would have to re-run per mutant — too slow
# to be practical, and CLAUDE.md's own coverage findings (see
# "Test coverage: what's actually thin" if present, or the 2026-08-30 session
# notes) showed the modules worth mutation-testing first are the ones that
# are both pure (or near-pure) AND already well-covered by fast, DB-free unit
# tests: hr_outliers.py, hr_statistics.py, hr_models.py, hr_sql_utils.py, and
# hr_corrections.py. A high line-coverage number and a high mutation-kill
# rate should agree for these; a mismatch means something (tests execute the
# code but do not assert on its behaviour). Widen this list deliberately, one
# module at a time, rather than assuming the DB-backed adapters would behave
# the same way against their own (slower, integration-level) test suites.
#
# ONE MODULE AT A TIME, NOT ALL AT ONCE
# Each module gets its own mutmut sandbox and results file, archived
# separately, so a crash or bad result in one module's run never corrupts or
# obscures another's — see the nibe-smo-mqtt-bridge-local script this was
# adapted from for the incident that established this pattern.
#
# USAGE
#   ./run-mutmut.sh              — run every module below, sequentially
#   ./run-mutmut.sh <module>     — run just one module (e.g. hr_outliers)
#   ./run-mutmut.sh --list       — print the module list and exit
#
# Each module's mutmut run produces:
#   mutmut-results/<module>.txt        — full `mutmut results` output
#   mutmut-results/SUMMARY.txt         — one line per module (survived/total),
#                                         appended to across the whole run
#   mutants-<module>/                  — that module's own full mutmut
#                                         sandbox, archived (not shared/
#                                         overwritten) so `mutmut show` stays
#                                         available for EVERY module later,
#                                         not just whichever ran last
#
# INSPECTING AN ARCHIVED MODULE'S MUTANTS LATER
#   mutmut hardcodes the literal directory name "mutants" relative to cwd
#   throughout its own source — there is no config option to rename or
#   relocate it, so `mutmut show`/`mutmut results` only ever read from
#   ./mutants. To inspect a module that isn't the one most recently run:
#     rm -rf mutants && cp -r mutants-<module> mutants && mutmut show <id>
#   (cp, not mv, so the archive under mutants-<module>/ survives for next
#   time too.)
#
# RESUMING AN INTERRUPTED RUN
# A module's mutants/ sandbox is only renamed to mutants-<module>/ after
# that module's run finishes — an interrupted run leaves a half-finished
# plain mutants/ behind, which the next invocation of this script discards
# as stale before starting fresh. To resume just the interrupted module, run
# it by name again — it starts over from scratch, not from where it left off.
#
# INTERPRETING SURVIVORS
#   Add a test that pins the exact value/condition, or annotate the line
#   with a comment documenting genuinely equivalent mutations (e.g. log
#   strings whose case doesn't affect an otherwise case-insensitive match).
#
# WHY -n0 IS IN pytest_add_cli_args BELOW
#   mutmut's stats-collection plugin (which tracks "which test hit which
#   mutated function", used to decide which tests to re-run per mutant)
#   registers hits via a pytest plugin hook (pytest_runtest_teardown) into a
#   process-global dict. Under pytest-xdist (this project's own pytest.ini
#   sets addopts = -n auto), that hook fires inside each xdist WORKER
#   subprocess, and the resulting data never gets marshalled back to the
#   controller process mutmut itself runs in — so mutmut sees zero hits for
#   every mutant and aborts immediately with "could not find any test case
#   for any mutant", even though the tests genuinely exercise the mutated
#   code and pass normally. Confirmed directly: a manual probe with
#   MUTANT_UNDER_TEST=stats set by hand showed the trampoline instrumentation
#   correctly recording a hit when run without xdist, and no hit (silently)
#   when xdist distributed the same test across a worker. -n0 disables xdist
#   for mutmut's own pytest invocations only — the project's normal `pytest`
#   invocation (via pytest.ini's addopts) is completely unaffected, and each
#   module's own test set here is small enough that losing parallelism for
#   mutmut's runs costs little.
#
# ⚠ MUTMUT RELIABILITY CAVEAT — READ BEFORE TRUSTING ANY OUTPUT HERE ⚠
#   mutmut 3.7.0 calls pytest.main() IN-PROCESS — once for its own baseline
#   "which tests cover which mutants" stats collection, then again per
#   mutant tested, all in the same worker process. Calling pytest.main()
#   more than once in the same interpreter is explicitly unsupported by
#   pytest and has been observed (on the sibling nibe project this script
#   was adapted from) to corrupt pytest's own tmp_path_factory cleanup,
#   crashing that worker and producing false "no tests"/"survived" verdicts.
#   Splitting into one-module-per-run reduces the blast radius but does NOT
#   eliminate false "survived" verdicts even in a single-module isolated
#   run. CONCLUSION: treat every "survived" verdict in mutmut-results/*.txt
#   as an unverified hypothesis, not a fact. Before writing a test for one,
#   independently apply the exact diff from `mutmut show <id>` by hand to
#   app/<module>.py and confirm with a PLAIN `pytest tests/test_<x>.py -q`
#   run (not through mutmut) that the real suite does not already catch it
#   — and after fixing a batch, spot-check a couple of "killed" mutants the
#   same way too, since a corrupted run's false negatives are just as
#   unverified as its false positives. `mutmut show <id>` itself remains
#   reliable (it only applies a patch and prints the diff — no test
#   execution involved); it is specifically the coverage-collection/
#   survived-verdict machinery that is compromised.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/mutmut-results"
ARG="${1:-}"

# MODULE LIST
# module_name|test_file1,test_file2,...
# Test-file sets carry forward every DB-free unit test file known to touch
# the module (never narrowed to a single guessed-relevant file), so no
# module silently loses coverage from tests in another file that happen to
# exercise it — hr_models.py in particular is constructed all over the unit
# suite, not just in one dedicated test file.
MODULES=(
    "hr_outliers|tests/test_outliers.py"
    "hr_statistics|tests/test_statistics.py"
    "hr_sql_utils|tests/test_mariadb.py"
    "hr_corrections|tests/test_corrections.py,tests/test_web.py"
    "hr_state|tests/test_state.py"
    "hr_discovery|tests/test_discovery.py"
    "hr_mqtt|tests/test_mqtt.py"
)
# hr_config.py was tried and dropped for the same reason as hr_models.py, but
# a different root cause: mutmut 3.7.0 generates zero mutants for its one
# piece of real logic, AppConfig.from_env, because that method is decorated
# with @classmethod — confirmed by checking mutants-hr_config/hr_config.py
# directly, which shows from_env copied verbatim with no x*__mutmut_N
# variants at all, unlike every plain function/instance method in this
# project's other modules. This is a mutmut limitation with @classmethod, not
# a signal that from_env is untested — do not re-add expecting a real result
# unless a later mutmut version is confirmed to handle classmethods.
# hr_models.py was tried and dropped: mutmut generates zero mutants for it
# (confirmed via mutants-hr_models/hr_models.py.meta: hash_by_function_name is
# empty) because it is pure dataclasses/Enums with no executable logic to
# mutate. Its "stopping early, no test case for any mutant" message is the
# correct response to there being nothing to test, not the xdist bug above —
# do not re-add it expecting a real result.
# hr_db.py was tried and dropped for the identical reason: it is entirely
# @abstractmethod stubs and exception class definitions (confirmed via
# mutants-hr_db/hr_db.py.meta: hash_by_function_name empty) — no executable
# logic anywhere in the file, so there is nothing for mutmut to mutate.

if [ "$ARG" = "--list" ]; then
    for entry in "${MODULES[@]}"; do
        echo "${entry%%|*}"
    done
    exit 0
fi

# VENV DISCOVERY
for _venv_dir in ".venv-check" ".venv" "venv"; do
    if [ -x "$SCRIPT_DIR/$_venv_dir/bin/mutmut" ]; then
        PATH="$SCRIPT_DIR/$_venv_dir/bin:$PATH"
        echo "[mutmut] Using venv: $_venv_dir/"
        break
    fi
done
unset _venv_dir

if ! command -v mutmut >/dev/null 2>&1; then
    echo "[mutmut] ERROR: 'mutmut' not found on PATH and no venv with it was" >&2
    echo "  found at $SCRIPT_DIR/{.venv-check,.venv,venv}/bin/mutmut." >&2
    echo "  Activate the project venv first, or install mutmut into one of" >&2
    echo "  those locations." >&2
    exit 1
fi

mkdir -p "$RESULTS_DIR"

run_one_module() {
    local module="$1"
    local test_files_csv="$2"
    IFS=',' read -ra test_files <<< "$test_files_csv"

    echo ""
    echo "=================================================================="
    echo "[mutmut] Module: ${module}.py"
    echo "=================================================================="

    cp "$SCRIPT_DIR/app/${module}.py" "$SCRIPT_DIR/${module}.py"
    echo "[mutmut] Staged: ${module}.py (copy of app/${module}.py — kept for mutmut show)"

    {
        echo "[tool.mutmut]"
        echo "source_paths = [\"${module}.py\"]"
        echo "also_copy = ["
        echo "    \"tests\","
        echo "    \"pytest.ini\","
        echo "    \"app\","
        echo "]"
        echo "pytest_add_cli_args = ["
        echo "    \"--timeout=600\","
        echo "    \"--override-ini=pythonpath=. app tests\","
        echo "    \"-p\", \"no:randomly\","
        echo "    \"-n0\","
        echo "]"
        echo "pytest_add_cli_args_test_selection = ["
        for tf in "${test_files[@]}"; do
            echo "    \"${tf}\","
        done
        echo "]"
        echo "mutate_only_covered_lines = false"
        echo "timeout_multiplier = 5.0"
        echo "timeout_constant = 30.0"
    } > "$SCRIPT_DIR/pyproject.toml"

    # Any plain mutants/ left over here can only be a half-finished sandbox
    # from an interrupted earlier run (a completed run always renames it
    # away to mutants-<module>/ below before this point is reached again) —
    # safe to discard.
    echo "[mutmut] Removing any stale mutants/ sandbox (interrupted-run leftover)..."
    rm -rf "$SCRIPT_DIR/mutants"

    cd "$SCRIPT_DIR"
    if [ -n "${MUTMUT_MAX_CHILDREN:-}" ]; then
        echo "[mutmut] Capping worker concurrency: --max-children ${MUTMUT_MAX_CHILDREN}"
        mutmut run --max-children "$MUTMUT_MAX_CHILDREN" || true
    else
        mutmut run || true
    fi

    local results_file="$RESULTS_DIR/${module}.txt"
    mutmut results > "$results_file" 2>&1 || true
    local survived
    survived="$(grep -c ': survived' "$results_file" || true)"
    local no_tests
    no_tests="$(grep -c ': no tests' "$results_file" || true)"
    local total
    total="$(wc -l < "$results_file" | tr -d ' ')"
    echo "[mutmut] ${module}.py: ${survived} survived, ${no_tests} no-tests, ${total} lines total -> ${results_file}"
    echo "$(date '+%Y-%m-%d %H:%M:%S')  ${module}  survived=${survived}  no_tests=${no_tests}  results_lines=${total}" >> "$RESULTS_DIR/SUMMARY.txt"

    # Archive this module's own mutants/ sandbox under a per-module name
    # BEFORE the next module's run wipes plain mutants/ — otherwise `mutmut
    # show` only ever works for whichever module ran last, since mutmut
    # hardcodes the literal directory name "mutants" relative to cwd (no
    # config option to rename/relocate it). Each module keeps its own full
    # sandbox this way, inspectable later via:
    #   rm -rf mutants && cp -r mutants-<module> mutants && mutmut show <id>
    if [ -d "$SCRIPT_DIR/mutants" ]; then
        rm -rf "$SCRIPT_DIR/mutants-${module}"
        mv "$SCRIPT_DIR/mutants" "$SCRIPT_DIR/mutants-${module}"
        echo "[mutmut] Archived sandbox: mutants-${module}/ (for later 'mutmut show' access)"
    fi
}

if [ -n "$ARG" ]; then
    found=""
    for entry in "${MODULES[@]}"; do
        name="${entry%%|*}"
        if [ "$name" = "$ARG" ]; then
            found="$entry"
            break
        fi
    done
    if [ -z "$found" ]; then
        echo "[mutmut] ERROR: unknown module '$ARG'. Run './run-mutmut.sh --list' to see valid names." >&2
        exit 1
    fi
    run_one_module "${found%%|*}" "${found#*|}"
else
    echo "[mutmut] No module given — running all ${#MODULES[@]} modules sequentially."
    for entry in "${MODULES[@]}"; do
        run_one_module "${entry%%|*}" "${entry#*|}"
    done
    # Clean up root-level staging copies and the generated pyproject.toml
    # after a full run so they can't shadow app/ during a later plain
    # `pytest` invocation.
    echo ""
    echo "[mutmut] Full run complete — removing root-level staging copies..."
    for entry in "${MODULES[@]}"; do
        rm -f "$SCRIPT_DIR/${entry%%|*}.py"
    done
    rm -f "$SCRIPT_DIR/pyproject.toml"
fi

echo ""
echo "[mutmut] Done. Results saved under: ${RESULTS_DIR}/"
echo "  cat ${RESULTS_DIR}/SUMMARY.txt                       — one line per module run this session"
echo "  cat ${RESULTS_DIR}/<module>.txt                      — full mutmut results for one module"
echo "  rm -rf mutants && cp -r mutants-<module> mutants      — restore a module's sandbox for 'mutmut show'"
echo "  mutmut show <mutant_id>                              — diff for a specific mutant (after restoring above)"
echo ""
echo "Remember the reliability caveat at the top of this script: verify"
echo "every 'survived' result by hand before trusting it."
