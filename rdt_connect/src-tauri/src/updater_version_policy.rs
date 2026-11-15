//! Prevents an updater feed from crossing platform build-marker namespaces.

fn marker_family(marker: &str) -> Option<u8> {
    let b = marker.as_bytes();
    match b {
        [platform @ b'1'..=b'3', series, d1, d2]
            if series.is_ascii_uppercase() && d1.is_ascii_digit() && d2.is_ascii_digit() =>
        {
            Some(*platform)
        }
        [platform @ (b'4' | b'5'), b'L', d1, d2, d3]
            if d1.is_ascii_digit() && d2.is_ascii_digit() && d3.is_ascii_digit() =>
        {
            Some(*platform)
        }
        _ => None,
    }
}

/// Accept only newer SemVer candidates in the installed platform family.
pub(crate) fn candidate_is_acceptable(
    current_version: &str,
    current_build_id: &str,
    candidate_version: &str,
) -> bool {
    let (Ok(current), Ok(candidate)) = (
        semver::Version::parse(current_version),
        semver::Version::parse(candidate_version),
    ) else {
        return false;
    };
    if candidate <= current {
        return false;
    }

    let Some(current_family) = marker_family(current_build_id) else {
        return false;
    };

    if !current_version.ends_with(&format!(".{current_build_id}")) {
        return false;
    }

    let Some((_, candidate_marker)) = candidate_version.rsplit_once('.') else {
        return false;
    };

    let Some(candidate_family) = marker_family(candidate_marker) else {
        return false;
    };

    current_family == candidate_family
}

#[cfg(test)]
mod tests {
    use super::candidate_is_acceptable as matches;

    #[test]
    fn accepts_same_platform_and_series_rollover() {
        assert!(matches("0.2.0-dev.11.1D01", "1D01", "0.2.0-dev.11.1D02"));
        assert!(matches("0.2.0-dev.11.1D99", "1D99", "0.2.0-dev.11.1E00"));
        assert!(matches("0.2.0-dev.11.2A05", "2A05", "0.2.0-dev.11.2B01"));
        assert!(matches("0.2.0-dev.11.3A01", "3A01", "0.2.0-dev.11.3A02"));
        assert!(matches("0.2.0-dev.11.4L001", "4L001", "0.2.0-dev.11.4L002"));
        assert!(matches("0.2.0-dev.11.5L001", "5L001", "0.2.0-dev.11.5L002"));
    }

    #[test]
    fn rejects_cross_platform_candidates_even_if_semver_is_higher() {
        assert!(!matches("0.2.0-dev.11.1D01", "1D01", "0.2.0-dev.99.5L999"));
        assert!(!matches("0.2.0-dev.11.2A01", "2A01", "0.2.0-dev.99.3A99"));
        assert!(!matches(
            "0.2.0-dev.11.4L001",
            "4L001",
            "0.2.0-dev.99.5L999"
        ));
        assert!(!matches(
            "0.2.0-dev.11.5L001",
            "5L001",
            "0.2.0-dev.99.4L999"
        ));
    }

    #[test]
    fn accepts_marker_only_increment_without_dev_increment() {
        assert!(matches("0.2.0-dev.12.1D34", "1D34", "0.2.0-dev.12.1D35"));
    }

    #[test]
    fn accepts_dev_increment_with_new_marker() {
        assert!(matches("0.2.0-dev.12.1D34", "1D34", "0.2.0-dev.13.1D01"));
    }

    #[test]
    fn rejects_same_or_older_version_even_on_same_platform() {
        assert!(!matches("0.2.0-dev.12.1D34", "1D34", "0.2.0-dev.12.1D34"));
        assert!(!matches("0.2.0-dev.12.1D34", "1D34", "0.2.0-dev.11.1D99"));
    }

    #[test]
    fn accepts_newer_base_release_on_same_platform() {
        assert!(matches("0.2.0-dev.11.1D04", "1D04", "0.2.0-dev.12.1D34"));
        assert!(matches("0.2.0-dev.11.4L001", "4L001", "0.2.0-dev.12.4L002"));
    }

    #[test]
    fn rejects_current_version_with_wrong_installed_marker() {
        assert!(!matches("0.2.0-dev.11.1D03", "1D04", "0.2.0-dev.12.1D34"));
    }

    #[test]
    fn rejects_malformed_or_legacy_namespace_values() {
        assert!(!matches("0.2.0-dev.11.1D01", "1D1", "0.2.0-dev.11.1D02"));
        assert!(!matches(
            "0.2.0-dev.11.4U001",
            "4U001",
            "0.2.0-dev.11.4L002"
        ));
        assert!(!matches("0.2.0-dev.11.1D01", "1D01", "0.2.0-dev.11.4L001"));
        assert!(!matches("0.2.0-dev.11.1D02", "1D01", "0.2.0-dev.11.1D03"));
    }
}
