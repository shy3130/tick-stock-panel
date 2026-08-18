// Pi Agent Harness pilot worker.
//
// Speaks the line-delimited JSON protocol shared with the Python runtime:
//   startup        -> stdout {"type":"ready"}
//   Python         -> stdin  {"type":"start",...}   (exactly once)
//   worker         -> stdout {"type":"delta"|"tool_request"|"done"|"fatal"}
//   Python         -> stdin  {"type":"tool_result",...}
//
// stdout carries protocol JSON only; diagnostics go to redacted stderr.
// The API key exists only in memory and never reaches any output.

import { createInterface } from "node:readline";
import { writeSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { Agent } from "@earendil-works/pi-agent-core";
import { createModels, createProvider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { makeAgentTools, ToolBridge, ProtocolError } from "./tools.js";
import { parseStart, StartValidationError } from "./validate.js";
import { sanitizeFatalMessage, sanitizeText } from "./redact.js";

const PROVIDER_ID = "pi-pilot";

function emptyUsage() {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  };
}

function buildModel(profile) {
  const model = {
    id: profile.model,
    name: profile.model,
    api: "openai-completions",
    provider: PROVIDER_ID,
    baseUrl: profile.baseUrl,
    reasoning: profile.thinkingLevel !== "off",
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: profile.contextWindow,
    maxTokens: profile.maxTokens,
  };
  if (profile.temperature !== undefined) {
    model.samplingParams = { temperature: profile.temperature };
  }
  if (profile.extraHeaders) {
    model.headers = profile.extraHeaders;
  }
  if (profile.compatibility) {
    model.compat = profile.compatibility;
  }
  return model;
}

function historyToAgentMessages(messages) {
  const out = [];
  const now = Date.now();
  for (const { role, content } of messages) {
    if (role === "user") {
      out.push({ role: "user", content, timestamp: now });
    } else {
      out.push({
        role: "assistant",
        content: content.length > 0 ? [{ type: "text", text: content }] : [],
        api: "openai-completions",
        provider: PROVIDER_ID,
        model: "history",
        usage: emptyUsage(),
        stopReason: "stop",
        timestamp: now,
      });
    }
  }
  return out;
}

function defaultStreamFn(cfg, model) {
  const models = createModels();
  models.setProvider(
    createProvider({
      id: PROVIDER_ID,
      baseUrl: cfg.profile.baseUrl,
      auth: {
        apiKey: {
          name: "pi-pilot",
          // The key arrives via the start message; it is resolved from memory
          // and never read from disk or environment.
          resolve: async () => ({ auth: { apiKey: cfg.profile.apiKey }, source: "stdin" }),
        },
      },
      models: [model],
      api: openAICompletionsApi(),
    }),
  );
  const resolved = models.getModel(PROVIDER_ID, model.id);
  if (!resolved) {
    throw new Error("failed to register pilot provider model");
  }
  return models.streamSimple.bind(models);
}

/**
 * Wire the worker over the given streams. `deps.stdout` receives protocol
 * lines (one JSON object per line, newline-terminated). `deps.streamFn`
 * overrides the LLM stream function (tests); production uses the official
 * `models.streamSimple.bind(models)`.
 */
export function createWorker(deps = {}) {
  const stdin = deps.stdin ?? process.stdin;
  const stderr = deps.stderr ?? process.stderr;
  const injectedStreamFn = deps.streamFn;

  const lines = [];
  const write =
    deps.write ??
    ((line) => {
      // synchronous write: the line is on the wire before any exit path runs
      writeSync(1, `${line}\n`);
    });

  let state = "awaiting-start"; // awaiting-start | running | finished
  let exitCode = 0;
  let secrets = [];
  let bridge = null;
  let rl = null;
  let finishedResolver = null;
  const whenFinished = new Promise((resolve) => {
    finishedResolver = resolve;
  });

  function emitLine(envelope) {
    write(JSON.stringify(envelope));
    lines.push(envelope);
  }

  function fatal(message) {
    if (state === "finished") return;
    state = "finished";
    exitCode = 1;
    emitLine({ type: "fatal", message });
    stderr.write(`${sanitizeText(message, secrets)}\n`);
    if (bridge) bridge.disposeAll("worker failed");
    if (rl) rl.close();
    finishedResolver({ code: exitCode, lines });
  }

  function finishDone(startedAt) {
    if (state === "finished") return;
    state = "finished";
    exitCode = 0;
    emitLine({ type: "done", elapsed_ms: Date.now() - startedAt });
    if (bridge) bridge.disposeAll("worker finished");
    if (rl) rl.close();
    finishedResolver({ code: exitCode, lines });
  }

  function handleLine(raw) {
    const text = raw.trim();
    if (text.length === 0) return;
    let message;
    try {
      message = JSON.parse(text);
    } catch {
      fatal("stdin line is not valid JSON");
      return;
    }
    if (message === null || typeof message !== "object" || Array.isArray(message)) {
      fatal("stdin message must be a JSON object");
      return;
    }
    if (message.type === "start") {
      if (state !== "awaiting-start") {
        fatal("duplicate start message: worker accepts exactly one start");
        return;
      }
      onStart(message);
      return;
    }
    if (message.type === "tool_result") {
      if (state !== "running") {
        fatal("tool_result received outside a running attempt");
        return;
      }
      try {
        bridge.handleToolResult(message);
      } catch (err) {
        if (err instanceof ProtocolError) {
          fatal(sanitizeFatalMessage("protocol error", err, secrets));
          return;
        }
        throw err;
      }
      return;
    }
    fatal(`unsupported stdin message type ${JSON.stringify(message.type ?? null)}`);
  }

  function onStart(message) {
    let cfg;
    try {
      cfg = parseStart(message);
      if (cfg.messages.length === 0 && cfg.finalPrompt === null) {
        throw new StartValidationError("start has no input: provide messages or final_prompt");
      }
    } catch (err) {
      if (err instanceof StartValidationError) {
        fatal(sanitizeFatalMessage("invalid start", err, []));
        return;
      }
      throw err;
    }

    secrets = [cfg.profile.apiKey];
    bridge = new ToolBridge(emitLine, { secrets });

    let tools;
    try {
      tools = makeAgentTools(cfg.toolSpecs, bridge);
    } catch (err) {
      if (err instanceof ProtocolError) {
        fatal(sanitizeFatalMessage("invalid tools", err, secrets));
        return;
      }
      throw err;
    }

    // A zero tool-round budget means tools never enter the context at all.
    const maxToolTurns = cfg.maxToolRounds;
    if (maxToolTurns <= 0) {
      tools = [];
    }
    // Total LLM turn cap: the tool budget plus exactly one final answer turn.
    // Guards against a model hallucinating tool calls after tools were removed.
    const maxTotalTurns = maxToolTurns + 1;

    const model = buildModel(cfg.profile);
    const streamFn = injectedStreamFn ?? defaultStreamFn(cfg, model);

    const runState = { toolTurns: 0, totalTurns: 0 };
    const startedAt = Date.now();
    const initialSystemPrompt =
      tools.length === 0 && cfg.finalPrompt !== null ? cfg.finalPrompt : cfg.systemPrompt;
    let runFailure = null;

    const agent = new Agent({
      streamFn,
      toolExecution: "sequential",
      initialState: {
        systemPrompt: initialSystemPrompt,
        model,
        thinkingLevel: cfg.profile.thinkingLevel,
        tools,
        messages: [],
      },
      prepareNextTurnWithContext: (ctx) => {
        const message = ctx.message;
        const hadToolCalls =
          message !== null &&
          typeof message === "object" &&
          message.role === "assistant" &&
          Array.isArray(message.content) &&
          message.content.some((block) => block !== null && typeof block === "object" && block.type === "toolCall");
        if (hadToolCalls) {
          runState.toolTurns += 1;
        }
        if (runState.toolTurns >= maxToolTurns && (ctx.context.tools?.length ?? 0) > 0) {
          // Context replacement must retain the transcript. The final prompt
          // is a system instruction, never an extra user message.
          return {
            context: {
              ...ctx.context,
              systemPrompt: cfg.finalPrompt ?? ctx.context.systemPrompt,
              tools: [],
            },
          };
        }
        return undefined;
      },
      shouldStopAfterTurn: () => {
        runState.totalTurns += 1;
        return runState.totalTurns >= maxTotalTurns;
      },
    });

    agent.subscribe((event) => {
      if (event.type === "message_update" && event.assistantMessageEvent?.type === "text_delta") {
        // Only assistant text deltas are surfaced to the legacy protocol.
        emitLine({ type: "delta", content: event.assistantMessageEvent.delta });
        return;
      }
      if (
        (event.type === "message_end" || event.type === "turn_end") &&
        event.message !== null &&
        typeof event.message === "object" &&
        event.message.role === "assistant" &&
        (event.message.stopReason === "error" || event.message.stopReason === "aborted")
      ) {
        runFailure = event.message.errorMessage ?? `assistant turn ended with stopReason "${event.message.stopReason}"`;
        return;
      }
      if (event.type === "agent_end") {
        if (runFailure !== null) {
          fatal(sanitizeText(runFailure, secrets) || "agent run failed");
        } else {
          finishDone(startedAt);
        }
      }
    });

    state = "running";

    const promptMessages = historyToAgentMessages(cfg.messages);
    agent
      .prompt(promptMessages)
      .then(() => {
        if (state !== "finished") {
          fatal("agent ended without a terminal event");
        }
      })
      .catch((err) => {
        fatal(sanitizeFatalMessage("run error", err, secrets));
      });
  }

  rl = createInterface({ input: stdin, crlfDelay: Infinity });
  rl.on("line", handleLine);
  rl.on("close", () => {
    if (state === "running") {
      fatal("stdin closed before the attempt finished");
    }
  });
  rl.on("error", (err) => {
    fatal(sanitizeFatalMessage("stdin error", err, secrets));
  });

  emitLine({ type: "ready" });

  return {
    whenFinished,
    get state() {
      return state;
    },
    get lines() {
      return lines;
    },
  };
}
export function isSupportedNodeVersion(version = process.versions.node) {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(version);
  if (!match) return false;
  const major = Number(match[1]);
  const minor = Number(match[2]);
  return major > 22 || (major === 22 && minor >= 19);
}

function runProcessWorker() {
  if (!isSupportedNodeVersion()) {
    const message = "Pi Agent worker requires Node.js 22.19 or newer";
    writeSync(1, `${JSON.stringify({ type: "fatal", message })}\n`);
    process.stderr.write(`${message}\n`);
    process.exit(1);
    return;
  }
  const worker = createWorker();
  worker.whenFinished.then(
    (result) => {
      process.exit(result.code);
    },
    () => {
      process.exit(1);
    },
  );
}

const isMain =
  process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  runProcessWorker();
}
