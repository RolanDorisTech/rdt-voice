fn valid_marker(marker: &str) -> bool {
    let bytes = marker.as_bytes();
    bytes.len() == 4
        && matches!(bytes[0], b'1'..=b'5')
        && bytes[1].is_ascii_uppercase()
        && bytes[2].is_ascii_digit()
        && bytes[3].is_ascii_digit()
}

/// Checks platform/build namespace compatibility. Version ordering is checked
/// separately by the native Tauri comparator before this policy is called.
pub(crate) fn candidate_matches_installed_namespace(
    current_version: &str,
    current_build_id: &str,
    candidate_version: &str,
) -> bool {
    if !valid_marker(current_build_id) {
        return false;
    }

    if !current_version.ends_with(&format!(".{current_build_id}")) {
        return false;
    }

    let Some((_, candidate_marker)) = candidate_version.rsplit_once('.') else {
        return false;
    };

    if !valid_marker(candidate_marker) {
        return false;
    }

    let current = current_build_id.as_bytes();
    let candidate = candidate_marker.as_bytes();

    // First digit is the platform/architecture namespace.
    if candidate[0] != current[0] {
        return false;
    }

    // Linux's second character is the distribution identifier, not a
    // rollover family. Ubuntu must not receive a Debian build, or vice versa.
    if matches!(current[0], b'4' | b'5') && candidate[1] != current[1] {
        return false;
    }

    true
}

#[cfg(test)]
mod tests {
    use super::candidate_matches_installed_namespace as matches;

    fn version(marker: &str) -> String {
        format!("0.2.0-dev.11.{marker}")
    }

    #[test]
    fn accepts_only_matching_platform_and_architecture_namespaces() {
        assert!(matches(&version("1D01"), "1D01", &version("1D02")));
        assert!(matches(&version("1D01"), "1D01", &version("1E00")));
        assert!(!matches(&version("1D01"), "1D01", &version("2A04")));
        assert!(!matches(&version("1D01"), "1D01", &version("3A01")));

        assert!(matches(&version("2A05"), "2A05", &version("2A06")));
        assert!(matches(&version("2A99"), "2A99", &version("2B00")));
        assert!(!matches(&version("2A05"), "2A05", &version("1D02")));
        assert!(!matches(&version("2A05"), "2A05", &version("3A01")));

        assert!(matches(&version("3A01"), "3A01", &version("3A02")));
        assert!(!matches(&version("3A01"), "3A01", &version("2A99")));
    }

    #[test]
    fn linux_requires_matching_architecture_and_distribution() {
        assert!(matches(&version("4U01"), "4U01", &version("4U02")));
        assert!(!matches(&version("4U01"), "4U01", &version("4D02")));
        assert!(!matches(&version("4U01"), "4U01", &version("5U01")));

        assert!(matches(&version("5D01"), "5D01", &version("5D02")));
        assert!(!matches(&version("5D01"), "5D01", &version("5U02")));
        assert!(!matches(&version("5D01"), "5D01", &version("4D02")));
    }

    #[test]
    fn canonical_target_markers_never_cross_platform_or_architecture() {
        let targets = ["1D01", "2A05", "3A01", "4U01", "5U01"];

        // Each target may update within its own platform/architecture
        // namespace. Linux distribution letters remain part of that namespace.
        for marker in targets {
            assert!(
                matches(&version(marker), marker, &version(marker)),
                "same-target marker should be compatible: {marker}"
            );
        }

        for (i, current) in targets.iter().enumerate() {
            for (j, candidate) in targets.iter().enumerate() {
                if i != j {
                    assert!(
                        !matches(&version(current), current, &version(candidate)),
                        "cross-target update must be rejected: {current} -> {candidate}"
                    );
                }
            }
        }
    }

    #[test]
    fn linux_distribution_letters_must_match() {
        assert!(matches(&version("4U01"), "4U01", &version("4U02")));
        assert!(!matches(&version("4U01"), "4U01", &version("4D02")));
        assert!(matches(&version("5U01"), "5U01", &version("5U02")));
        assert!(!matches(&version("5U01"), "5U01", &version("5D02")));
    }

    #[test]
    fn invalid_or_inconsistent_markers_fail_closed() {
        assert!(!matches(&version("1D01"), "1D01", &version("2A.05")));
        assert!(!matches(&version("1D01"), "1D01", "0.2.0-dev.11"));
        assert!(!matches(&version("1D01"), "2A05", &version("1D02")));
        assert!(!matches(&version("0D01"), "0D01", &version("0D02")));
        assert!(!matches(
            "0.2.0-dev.11.1D.1",
            "1D01",
            &version("1D02")
        ));
    }
}
