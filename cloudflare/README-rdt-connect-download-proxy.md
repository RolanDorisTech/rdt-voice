# RDT Connect download progress Worker

This Worker streams only the named RDT Connect macOS release asset from the
official `RolanDorisTech/rdt-voice` GitHub Releases repository. It is not an
arbitrary-URL proxy. Browser CORS access is restricted to the official Pages
origin, `https://rolandoristech.github.io`.

## Deploy through Cloudflare Dashboard (no Wrangler)

1. Open **Cloudflare Dashboard → Workers & Pages**.
2. Create a Worker named `rdt-connect-download-proxy`.
3. Open its code editor, replace the starter code with the complete contents
   of `cloudflare/rdt-connect-download-proxy.mjs`, and deploy it.
4. Enable its `workers.dev` URL. The site is configured for
   `https://rdt-connect-download-proxy.rolandoristech.workers.dev`.
5. Verify `https://rdt-connect-download-proxy.rolandoristech.workers.dev/health`
   returns JSON with `"ok": true`.

Test a HEAD request against a current tag, for example:

`https://rdt-connect-download-proxy.rolandoristech.workers.dev/download/rdt-connect-v0.2.0-dev.10`

The Worker returns the GitHub asset stream; it does not store or buffer the
DMG itself. Only GET, HEAD, and OPTIONS are allowed. The endpoint accepts only
strictly formatted RDT Connect tags and derives the exact expected DMG filename.

## Browser progress behavior

On browsers supporting `showSaveFilePicker`, the page streams the response
chunk-by-chunk into a user-selected file and displays the number of bytes
written and percentage from the response length. Browsers without that API
use the normal GitHub download; their native Downloads UI reports progress.

The Worker must be deployed for in-page progress to work. If it is unavailable,
the page offers the normal direct GitHub download rather than claiming progress
that it cannot measure.
