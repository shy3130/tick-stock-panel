// Tool bridge: forwards Pi tool calls to Python over the worker protocol and
// awaits the matching tool_result. Tools are always executed sequentially
// (Python owns execution); this module only correlates request/response.

import { randomUUID } from "node:crypto";
import { Compile } from "typebox/compile";
import { jsonSchemaToTypeBox } from "./schema.js";
import { sanitizeText } from "./redact.js";
const TOOL_NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;

export class ProtocolError extends Error {
  constructor(message) {
    super(message);
    this.name = "ProtocolError";
  }
}

export class ToolExecutionError extends Error {
  constructor(message) {
    super(message);
    this.name = "ToolExecutionError";
  }
}

/**
 * Correlates tool_request messages with tool_result replies.
 * `send` is invoked with the protocol envelope to write to Python.
 */
export class ToolBridge {
  constructor(send, { secrets = [] } = {}) {
    this.send = send;
    this.pending = new Map();
    this.settled = new Set();
    this.disposed = false;
    this.secrets = secrets.filter((value) => typeof value === "string" && value.length >= 4);
  }

  /** Wait for the tool_result matching a tool call. Rejects on failure/abort. */
  requestToolCall({ toolCallId, name, args }, signal) {
    return new Promise((resolve, reject) => {
      const request_id = randomUUID();
      const entry = { resolve, reject, signal, onAbort: null };
      entry.onAbort = () => {
        if (this.pending.delete(request_id)) {
          this.settled.add(request_id);
          reject(new ToolExecutionError(`tool "${name}" aborted before result`));
        }
      };
      this.pending.set(request_id, entry);
      if (signal) {
        if (signal.aborted) {
          this.pending.delete(request_id);
          this.settled.add(request_id);
          reject(new ToolExecutionError(`tool "${name}" aborted before result`));
          return;
        }
        signal.addEventListener("abort", entry.onAbort, { once: true });
      }
      try {
        this.send({ type: "tool_request", request_id, tool_call_id: toolCallId, name, args });
      } catch (err) {
        this.pending.delete(request_id);
        if (signal) signal.removeEventListener("abort", entry.onAbort);
        reject(err);
      }
    });
  }

  /** Apply an inbound tool_result message. Throws ProtocolError on malformed input. */
  handleToolResult(message) {
    if (message === null || typeof message !== "object" || Array.isArray(message)) {
      throw new ProtocolError("tool_result must be an object");
    }
    const { request_id, ok, result, error } = message;
    if (typeof request_id !== "string" || request_id.length === 0) {
      throw new ProtocolError("tool_result.request_id must be a non-empty string");
    }
    const entry = this.pending.get(request_id);
    if (!entry) {
      if (this.disposed || this.settled.has(request_id)) {
        return; // late result after dispose or abort: ignore idempotently
      }
      throw new ProtocolError("tool_result references unknown request_id");
    }
    this.settled.add(request_id);
    this.pending.delete(request_id);
    if (entry.signal && entry.onAbort) {
      entry.signal.removeEventListener("abort", entry.onAbort);
    }
    if (ok === true) {
      entry.resolve(result === undefined ? null : result);
      return;
    }
    const reason = typeof error === "string" && error.length > 0 ? error : "tool execution failed";
    // Python already sanitizes tool errors; sanitize again defensively.
    entry.reject(new ToolExecutionError(sanitizeText(reason, this.secrets)));
  }

  /** Fail every pending waiter (stdin closed, fatal, shutdown). */
  disposeAll(reason) {
    this.disposed = true;
    const err = new ToolExecutionError(reason);
    for (const entry of this.pending.values()) {
      if (entry.signal && entry.onAbort) {
        entry.signal.removeEventListener("abort", entry.onAbort);
      }
      entry.reject(err);
    }
    this.pending.clear();
  }
}

const TOOL_SPEC_FIELDS = new Set(["name", "description", "input_schema", "read_only"]);

/**
 * Build Pi AgentTools from the protocol tool specs.
 * Fails closed on unknown tool shapes and unsupported schemas.
 */
export function makeAgentTools(toolSpecs, bridge) {
  if (!Array.isArray(toolSpecs)) {
    throw new ProtocolError("start.tools must be an array");
  }
  const seen = new Set();
  const tools = [];
  for (const spec of toolSpecs) {
    if (spec === null || typeof spec !== "object" || Array.isArray(spec)) {
      throw new ProtocolError("each tool must be an object");
    }
    for (const key of Object.keys(spec)) {
      if (!TOOL_SPEC_FIELDS.has(key)) {
        throw new ProtocolError(`tool spec has unknown field "${key}"`);
      }
    }
    const { name, description, input_schema, read_only } = spec;
    if (typeof name !== "string" || !TOOL_NAME_PATTERN.test(name)) {
      throw new ProtocolError(`tool name ${JSON.stringify(name)} is invalid`);
    }
    if (seen.has(name)) {
      throw new ProtocolError(`duplicate tool name "${name}"`);
    }
    if (typeof description !== "string" || description.length === 0) {
      throw new ProtocolError(`tool "${name}" must carry a non-empty description`);
    }
    if (read_only !== true) {
      // Defense in depth: the pilot only ever exposes read-only tools.
      throw new ProtocolError(`tool "${name}" is not marked read_only`);
    }
    let parameters;
    try {
      parameters = jsonSchemaToTypeBox(input_schema, `tool "${name}" input_schema`);
      // Compile once up front so malformed schemas fail the start message
      // instead of exploding mid-run.
      Compile(parameters);
    } catch (err) {
      throw new ProtocolError(
        `tool "${name}" schema is unsupported: ${sanitizeText(
          err instanceof Error ? err.message : String(err),
        )}`,
      );
    }
    seen.add(name);
    tools.push({
      name,
      label: name,
      description,
      parameters,
      executionMode: "sequential",
      async execute(toolCallId, params, signal) {
        const result = await bridge.requestToolCall({ toolCallId, name, args: params }, signal);
        return {
          content: [{ type: "text", text: resultToText(result) }],
          details: { value: result },
        };
      },
    });
  }
  return tools;
}

function resultToText(result) {
  if (typeof result === "string") return result;
  if (result === null || result === undefined) return "";
  try {
    return JSON.stringify(result);
  } catch {
    return String(result);
  }
}
