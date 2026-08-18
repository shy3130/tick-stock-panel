// Strict validation of the `start` message.
//
// Error messages describe the offending field path only — never the field
// value — so a malformed start can never echo the API key back.

export class StartValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = "StartValidationError";
  }
}

function fail(path, reason) {
  throw new StartValidationError(`start.${path}: ${reason}`);
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

const TOP_LEVEL_FIELDS = new Set([
  "type",
  "profile",
  "messages",
  "system_prompt",
  "final_prompt",
  "tools",
  "max_tool_rounds",
]);

const PROFILE_FIELDS = new Set([
  "provider",
  "base_url",
  "api_key",
  "model",
  "context_window",
  "max_tokens",
  "temperature",
  "thinking_level",
  "extra_headers",
  "compatibility",
]);

const THINKING_LEVELS = new Set(["off", "low", "medium", "high"]);

const COMPAT_BOOLEANS = new Set([
  "supportsStore",
  "supportsDeveloperRole",
  "supportsReasoningEffort",
  "supportsUsageInStreaming",
  "supportsFinishReason",
  "requiresToolResultName",
  "requiresAssistantAfterToolResult",
  "requiresThinkingAsText",
  "requiresReasoningContentOnAssistantMessages",
]);

const COMPAT_MAX_TOKENS_FIELDS = new Set(["max_completion_tokens", "max_tokens"]);

const COMPAT_THINKING_FORMATS = new Set([
  "openai",
  "openrouter",
  "deepseek",
  "together",
  "baseten",
  "zai",
  "qwen",
  "chat-template",
  "qwen-chat-template",
  "string-thinking",
  "ant-ling",
]);

const HEADER_NAME_PATTERN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;

function clampInt(value, field, min, max, fallback) {
  if (value === undefined || value === null) return fallback;
  if (typeof value !== "number" || !Number.isInteger(value)) {
    fail(field, "must be an integer");
  }
  if (value < min || value > max) {
    fail(field, `must be between ${min} and ${max}`);
  }
  return value;
}

/**
 * Parse and validate a start message body (already JSON-decoded, with
 * `type === "start"`). Returns a normalized configuration object.
 */
export function parseStart(message) {
  if (!isPlainObject(message)) fail("<root>", "must be an object");
  for (const key of Object.keys(message)) {
    if (!TOP_LEVEL_FIELDS.has(key)) fail(`<root>`, `unknown field "${key}"`);
  }

  const profile = message.profile;
  if (!isPlainObject(profile)) fail("profile", "must be an object");
  for (const key of Object.keys(profile)) {
    if (!PROFILE_FIELDS.has(key)) fail("profile", `unknown field "${key}"`);
  }

  if (profile.provider !== "openai_compat") {
    fail("profile.provider", 'must be "openai_compat"');
  }

  if (typeof profile.base_url !== "string" || profile.base_url.length === 0) {
    fail("profile.base_url", "must be a non-empty string");
  }
  let baseUrl;
  try {
    baseUrl = new URL(profile.base_url);
  } catch {
    fail("profile.base_url", "must be a valid URL");
  }
  if (baseUrl.protocol !== "http:" && baseUrl.protocol !== "https:") {
    fail("profile.base_url", "must use http or https");
  }

  if (typeof profile.api_key !== "string" || profile.api_key.length === 0) {
    fail("profile.api_key", "must be a non-empty string");
  }

  if (typeof profile.model !== "string" || profile.model.length === 0 || profile.model.length > 512) {
    fail("profile.model", "must be a non-empty string (max 512 chars)");
  }

  const contextWindow = clampInt(profile.context_window, "profile.context_window", 1024, 2_000_000, 128_000);
  const maxTokens = clampInt(profile.max_tokens, "profile.max_tokens", 1, 1_000_000, 8192);

  let temperature;
  if (profile.temperature !== undefined) {
    if (typeof profile.temperature !== "number" || !Number.isFinite(profile.temperature) || profile.temperature < 0 || profile.temperature > 2) {
      fail("profile.temperature", "must be a number between 0 and 2");
    }
    temperature = profile.temperature;
  }

  let thinkingLevel = "off";
  if (profile.thinking_level !== undefined && profile.thinking_level !== null) {
    if (typeof profile.thinking_level !== "string" || !THINKING_LEVELS.has(profile.thinking_level)) {
      fail("profile.thinking_level", 'must be one of "off", "low", "medium", "high"');
    }
    thinkingLevel = profile.thinking_level;
  }

  let extraHeaders;
  if (profile.extra_headers !== undefined && profile.extra_headers !== null) {
    if (!isPlainObject(profile.extra_headers)) fail("profile.extra_headers", "must be an object");
    for (const [name, value] of Object.entries(profile.extra_headers)) {
      if (!HEADER_NAME_PATTERN.test(name)) fail("profile.extra_headers", `invalid header name "${name}"`);
      if (typeof value !== "string") fail("profile.extra_headers", `header "${name}" value must be a string`);
    }
    extraHeaders = { ...profile.extra_headers };
  }

  let compatibility;
  if (profile.compatibility !== undefined && profile.compatibility !== null) {
    if (!isPlainObject(profile.compatibility)) fail("profile.compatibility", "must be an object");
    compatibility = {};
    for (const [key, value] of Object.entries(profile.compatibility)) {
      if (COMPAT_BOOLEANS.has(key)) {
        if (typeof value !== "boolean") fail("profile.compatibility", `"${key}" must be a boolean`);
        compatibility[key] = value;
      } else if (key === "maxTokensField") {
        if (!COMPAT_MAX_TOKENS_FIELDS.has(value)) fail("profile.compatibility", `"${key}" must be "max_completion_tokens" or "max_tokens"`);
        compatibility[key] = value;
      } else if (key === "thinkingFormat") {
        if (!COMPAT_THINKING_FORMATS.has(value)) fail("profile.compatibility", `"${key}" has an unsupported value`);
        compatibility[key] = value;
      } else {
        fail("profile.compatibility", `unknown field "${key}"`);
      }
    }
  }

  if (!Array.isArray(message.messages)) fail("messages", "must be an array");
  const messages = [];
  for (let i = 0; i < message.messages.length; i += 1) {
    const entry = message.messages[i];
    if (!isPlainObject(entry)) fail(`messages[${i}]`, "must be an object");
    const { role, content } = entry;
    if (role !== "user" && role !== "assistant") fail(`messages[${i}].role`, 'must be "user" or "assistant"');
    if (typeof content !== "string") fail(`messages[${i}].content`, "must be a string");
    for (const key of Object.keys(entry)) {
      if (key !== "role" && key !== "content") fail(`messages[${i}]`, `unknown field "${key}"`);
    }
    messages.push({ role, content });
  }

  let systemPrompt = "";
  if (message.system_prompt !== undefined && message.system_prompt !== null) {
    if (typeof message.system_prompt !== "string") fail("system_prompt", "must be a string or null");
    systemPrompt = message.system_prompt;
  }

  let finalPrompt = null;
  if (message.final_prompt !== undefined && message.final_prompt !== null) {
    if (typeof message.final_prompt !== "string") fail("final_prompt", "must be a string or null");
    finalPrompt = message.final_prompt;
  }

  if (!Array.isArray(message.tools)) fail("tools", "must be an array");
  const maxToolRounds = clampInt(message.max_tool_rounds, "max_tool_rounds", 0, 5, 5);

  return {
    profile: {
      baseUrl: profile.base_url,
      apiKey: profile.api_key,
      model: profile.model,
      contextWindow,
      maxTokens,
      temperature,
      thinkingLevel,
      extraHeaders,
      compatibility,
    },
    messages,
    systemPrompt,
    finalPrompt,
    toolSpecs: message.tools,
    maxToolRounds,
  };
}
