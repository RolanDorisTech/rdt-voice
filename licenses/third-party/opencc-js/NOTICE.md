# OpenCC-JS third-party notice

RDT Voice uses **OpenCC-JS 1.4.2**, the pure-JavaScript Open Chinese Convert implementation, for optional local Simplified/Traditional Chinese output formatting.

- Upstream project: https://github.com/nk2028/opencc-js
- Pinned package: https://www.npmjs.com/package/opencc-js/v/1.4.2
- JavaScript package license: MIT; see `LICENSE`.
- Bundled dictionary data is generated from `opencc-data` and distributed under Apache License 2.0. See `LICENSES/Apache-2.0.txt` and the upstream `THIRD_PARTY_LICENSES.md`.

The converter runs locally in the browser after the bundle has been downloaded with this script. RDT Voice does not send transcript text to a translation service for this conversion. OpenCC changes writing conventions and some region-specific terms; it is not a translation service, and important transcripts should still be reviewed.

The runtime file is `assets/opencc-js-1.4.2-full.js`, with identical copies for Alpha and Beta testing. This script downloads the pinned runtime and its upstream license notices.
