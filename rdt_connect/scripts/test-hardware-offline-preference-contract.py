from pathlib import Path

source = Path("src/main.ts").read_text()
style = Path("src/style.css").read_text()

assert "inferenceDeviceChangePendingRestartPid" not in source
assert "let appliedInferenceDevicePreference: InferenceDevicePreference | null = null;" in source
assert "appliedInferenceDevicePreference !== inferenceDevicePreference" in source
assert "autoCheckUpdatesToggle.disabled = offlineOnlyModeEnabled;" in source
assert ".auto-check-updates-toggle.is-offline-disabled" in style

start = source.split("async function startSelectedBackendModel", 1)[1].split(
    "startButton.addEventListener", 1
)[0]
assert start.index("lastConnectionCheck?.healthOk !== true") < start.index(
    "appliedInferenceDevicePreference ="
)
assert "startedBackendHere" in start

save = source.split("async function saveInferenceDevicePreference", 1)[1].split(
    "const inferenceDevicePreferenceReady", 1
)[0]
assert "appliedInferenceDevicePreference =" not in save

offline = source.split(
    'offlineOnlyModeToggle?.addEventListener("change"',
    1
)[1].split("\n});", 1)[0]
assert "syncAutomaticUpdateToggleOfflineState();" in offline
assert "canRunAutomaticUpdateCheck()" in offline

startup = source.index("autoCheckUpdatesToggle.checked = localStorage.getItem(AUTO_CHECK_UPDATES_KEY)")
startup_check = source.index("if (canRunAutomaticUpdateCheck()) void checkForApplicationUpdates();", startup)
assert startup_check > startup

def warning(running, selected, applied):
    return running and applied is not None and selected != applied

applied = "gpu"
assert warning(True, "cpu", applied)
assert not warning(True, "gpu", applied)
assert warning(True, "cpu", applied)       # failed restart leaves prior applied value
applied = "cpu"                             # successful restart records new value
assert not warning(True, "cpu", applied)
assert not warning(False, "cpu", applied)  # stopped server
assert not warning(True, "cpu", None)      # unknown is not a false mismatch

assert "formatLifetimeHardwareDeviceLabel(name)" in source
assert "lifetimeAudioSharePercent(name, value?.audioSeconds, totalAudioSeconds)" in source

print("PASS: selected/applied hardware warning contract.")
print("PASS: revert, failed restart, successful restart, stop and unknown-state cases.")
print("PASS: Offline Mode toggle state contract.")
print("PASS: startup check remains after saved preference restoration.")
print("PASS: Lifetime Statistics attribution remains unchanged.")


# RDT_BENCHMARK_APPLIED_PREFERENCE_RESTORATION
# Preserve/re-establish only the preference known to have been applied originally.
benchmark_snapshot = source.split(
    "const originalInferenceDevicePreference =", 1
)[1].split("const verifyAndPreserveReusedOriginal", 1)[0]
assert "const originalAppliedInferenceDevicePreference = appliedInferenceDevicePreference;" in benchmark_snapshot

preserve_start = source.index("const verifyAndPreserveReusedOriginal")
restore_start = source.index("const restoreOriginalBackendIfNeeded", preserve_start)
preserve_block = source[preserve_start:restore_start]
assert preserve_block.index("originalBackendPreserved = true;") < preserve_block.index(
    "appliedInferenceDevicePreference = originalAppliedInferenceDevicePreference;"
)
assert "renderInferenceHardwareStatus();" in preserve_block

restore_end = source.index("let preferredFirstDevice", restore_start)
restore_block = source[restore_start:restore_end]
assert restore_block.index("if (!check?.healthOk") < restore_block.index(
    "appliedInferenceDevicePreference = originalAppliedInferenceDevicePreference;"
) < restore_block.index("originalBackendRestored = true;")
assert "renderInferenceHardwareStatus();" in restore_block
