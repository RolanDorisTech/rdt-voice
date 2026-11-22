from pathlib import Path

source = Path("src/main.ts").read_text(encoding="utf-8")
flow = source.split("async function runHardwareOptimizationFlowReserved(", 1)[1].split(
    "\nasync function ", 1
)[0]

assert '? 180_000' in flow
assert 'benchmarkDeadline - 15_000' in flow
assert 'candidateStartWindowClosed' in flow
assert 'Math.min(candidateStartedAt + 60_000, benchmarkDeadline)' in flow
assert 'manualFiniteCandidateCount' not in flow
assert 'remainingCandidateBudgetMs < 20_000' not in flow
assert 'manualHardwareSelection?.timeLimitEnabled' in flow
assert 'manualHardwareSelection?.fixture === "21s" ? 120_000 : 60_000' in flow
print("PASS: mandatory 180-second total, 60-second candidate cap and 15-second cutoff.")
print("PASS: manual options are isolated from mandatory first-run timing.")
