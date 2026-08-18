// Error/diagnostic redaction.
//
// The API key arrives only via the start message on stdin and must never
// reach stdout, stderr, or exit codes. All worker-originated messages and
// stderr diagnostics pass through sanitizeText with the profile secrets.

const MAX_LENGTH = 1200;

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// POSIX absolute paths with at least two segments, and Windows drive paths.
// Home-relative paths (~) are left alone: they carry no filesystem layout.
const ABSOLUTE_PATH_PATTERN = /(^|[\s'"`(=:])(?:\/[A-Za-z0-9._@-]+){2,}(?:\/)?|[A-Za-z]:\\(?:[A-Za-z0-9._@-]+\\)+/g;

/**
 * Redact secrets and absolute filesystem paths from a diagnostic string.
 * Whitespace runs are collapsed and the result is length-capped so raw
 * provider bodies (HTML error pages, JSON dumps) cannot leak wholesale.
 */
export function sanitizeText(text, secrets = []) {
  let out = typeof text === "string" ? text : String(text ?? "");
  for (const secret of secrets) {
    if (typeof secret === "string" && secret.length >= 4) {
      out = out.split(secret).join("***");
    }
  }
  out = out.replace(ABSOLUTE_PATH_PATTERN, (match, lead) => {
    // keep the leading delimiter, replace the path itself
    if (match.startsWith(lead) && lead !== "") {
      return `${lead}<path>`;
    }
    return "<path>";
  });
  out = out.replace(/\s+/g, " ").trim();
  if (out.length > MAX_LENGTH) {
    out = `${out.slice(0, MAX_LENGTH)}…`;
  }
  return out;
}

/**
 * Normalize an unknown thrown value into a sanitized one-line fatal message.
 * Never includes stack traces (they carry absolute paths and frame noise).
 */
export function sanitizeFatalMessage(kind, error, secrets = []) {
  const raw = error instanceof Error ? error.message : String(error ?? "");
  const message = sanitizeText(raw, secrets);
  return `pi worker ${kind}: ${message || "unknown error"}`;
}

