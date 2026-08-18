// Node worker contract tests: real Pi validation path, IPC single-start,
// tool request/result round-trip, five-tool-round budget, and redaction.

import test from "node:test";
import assert from "node:assert/strict";
import { PassThrough } from "node:stream";
import { validateToolArguments } from "@earendil-works/pi-ai";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { jsonSchemaToTypeBox, SchemaConversionError } from "../src/schema.js";
import { makeAgentTools, ToolBridge, ProtocolError } from "../src/tools.js";
import { createWorker, isSupportedNodeVersion } from "../src/worker.js";

const API_KEY = "sk-pilot-secret-0123456789abcdef";

function usage() {
  return {
    input: 1,
    output: 1,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: 2,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  };
}

function assistantMessage(content, stopReason) {
  return {
    role: "assistant",
    content,
    api: "openai-completions",
    provider: "pi-pilot",
    model: "test-model",
    usage: usage(),
    stopReason,
    timestamp: Date.now(),
  };
}

/**
 * Fake StreamFn speaking the real AssistantMessageEvent protocol.
 * While tools are present the "model" requests a tool call each turn;
 * once tools are removed it answers with plain text.
 */
function makeFakeStream() {
  const calls = [];
  const streamFn = (model, context) => {
    calls.push({
      toolCount: (context.tools ?? []).length,
      messageCount: context.messages.length,
    });
    const stream = createAssistantMessageEventStream();
    const hasTools = (context.tools ?? []).length > 0;
    const queue = hasTools
      ? [
          { type: "start", partial: assistantMessage([], "pending") },
          {
            type: "toolcall_end",
            contentIndex: 0,
            partial: assistantMessage(
              [{ type: "toolCall", id: `call_${calls.length}`, name: "get_quote", arguments: { symbol: "AAPL" } }],
              "pending",
            ),
            toolCall: { type: "toolCall", id: `call_${calls.length}`, name: "get_quote", arguments: { symbol: "AAPL" } },
          },
        ]
      : [
          { type: "start", partial: assistantMessage([], "pending") },
          { type: "text_delta", contentIndex: 0, delta: "final answer", partial: assistantMessage([{ type: "text", text: "" }], "pending") },
        ];
    const final = hasTools
      ? assistantMessage(
          [{ type: "toolCall", id: `call_${calls.length}`, name: "get_quote", arguments: { symbol: "AAPL" } }],
          "toolUse",
        )
      : assistantMessage([{ type: "text", text: "final answer" }], "stop");
    queue.push({
      type: "done",
      reason: hasTools ? "toolUse" : "stop",
      message: final,
    });
    for (const event of queue) {
      stream.push(event);
    }
    return stream;
  };
  return { streamFn, calls };
}

function makeHarness({ streamFn, toolReplies = [] } = {}) {
  const stdin = new PassThrough();
  const stderrChunks = [];
  const stderr = { write: (chunk) => stderrChunks.push(String(chunk)) };
  const lines = [];
  const write = (line) => lines.push(JSON.parse(line));
  const worker = createWorker({ stdin, stderr, write, streamFn });
  const send = (message) => stdin.write(`${JSON.stringify(message)}\n`);
  return { worker, stdin, send, lines, stderrChunks };
}
async function waitForLine(harness, predicate, timeoutMs = 2_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const match = harness.lines.find(predicate);
    if (match) return match;
    const terminal = harness.lines.find((line) => line.type === "done" || line.type === "fatal");
    if (terminal) {
      throw new Error(`worker terminated before expected line: ${JSON.stringify(harness.lines)}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`timed out waiting for worker line: ${JSON.stringify(harness.lines)}`);
}

function startMessage(overrides = {}) {
  return {
    type: "start",
    profile: {
      provider: "openai_compat",
      base_url: "https://gw.example.test/v1",
      api_key: API_KEY,
      model: "test-model",
      ...overrides.profile,
    },
    messages: [{ role: "user", content: "hi" }],
    system_prompt: "be brief",
    final_prompt: null,
    tools: [
      {
        name: "get_quote",
        description: "get a stock quote",
        input_schema: {
          type: "object",
          properties: {
            symbol: { type: "string", minLength: 1 },
            limit: { type: "integer", minimum: 1, maximum: 10 },
          },
          required: ["symbol"],
          additionalProperties: false,
        },
        read_only: true,
      },
    ],
    max_tool_rounds: 5,
    ...overrides,
  };
}

test("schema conversion: real Pi validation accepts missing optional and rejects missing required", () => {
  const schema = jsonSchemaToTypeBox({
    type: "object",
    properties: {
      symbol: { type: "string" },
      limit: { type: "integer" },
      tags: { type: "array", items: { type: "string" } },
      kind: { enum: ["a", "b"] },
    },
    required: ["symbol"],
  });
  // official Type.Object shape: required on the object, no property markers
  assert.deepEqual(schema.required, ["symbol"]);
  assert.equal(schema.properties.limit.optional, undefined);

  const tool = { name: "get_quote", description: "d", parameters: schema };
  const ok = validateToolArguments(tool, { type: "toolCall", id: "t1", name: "get_quote", arguments: { symbol: "AAPL" } });
  assert.deepEqual(ok, { symbol: "AAPL" }); // missing optional passes
  assert.throws(
    () => validateToolArguments(tool, { type: "toolCall", id: "t2", name: "get_quote", arguments: { limit: 3 } }),
    (err) => /symbol/.test(err.message),
  ); // missing required fails through the real path
  assert.throws(
    () => validateToolArguments(tool, { type: "toolCall", id: "t3", name: "get_quote", arguments: { symbol: "AAPL", tags: ["x"], junk: 1 } }),
    (err) => /junk|additional/i.test(err.message),
  ); // closed object: unknown key fails
});

test("schema conversion preserves the screener's unconstrained value field", () => {
  const parameters = jsonSchemaToTypeBox({
    type: "object",
    properties: { value: {} },
    required: ["value"],
    additionalProperties: false,
  });
  const tool = { name: "screen", description: "d", parameters };
  assert.deepEqual(
    validateToolArguments(tool, {
      type: "toolCall",
      id: "t-any",
      name: "screen",
      arguments: { value: [1, "x", true] },
    }),
    { value: [1, "x", true] },
  );
});

test("schema conversion preserves an array with unconstrained items", () => {
  const parameters = jsonSchemaToTypeBox({
    type: "object",
    properties: { symbols: { type: "array" } },
    required: ["symbols"],
    additionalProperties: false,
  });
  const tool = { name: "optimize", description: "d", parameters };
  assert.deepEqual(
    validateToolArguments(tool, {
      type: "toolCall",
      id: "t-array",
      name: "optimize",
      arguments: { symbols: ["600000.SH", 1] },
    }),
    { symbols: ["600000.SH", 1] },
  );
  assert.throws(() =>
    validateToolArguments(tool, {
      type: "toolCall",
      id: "t-array-invalid",
      name: "optimize",
      arguments: { symbols: "600000.SH" },
    }),
  );
});

test("worker enforces the documented Node runtime floor", () => {
  assert.equal(isSupportedNodeVersion("22.18.9"), false);
  assert.equal(isSupportedNodeVersion("22.19.0"), true);
  assert.equal(isSupportedNodeVersion("23.0.0"), true);
  assert.equal(isSupportedNodeVersion("invalid"), false);
});

test("schema conversion fails closed on unsupported constructs", () => {
  assert.throws(() => jsonSchemaToTypeBox({ $ref: "#/x" }), SchemaConversionError);
  assert.throws(() => jsonSchemaToTypeBox({ anyOf: [{ type: "string" }, { type: "number" }] }), SchemaConversionError);
  assert.throws(() => jsonSchemaToTypeBox({ type: "array", items: [{ type: "string" }] }), SchemaConversionError);
  assert.throws(() => jsonSchemaToTypeBox({ type: "object", required: ["missing"] }), SchemaConversionError);
});

test("worker: ready, single start, deltas, done; stdout stays protocol-only", async () => {
  const { streamFn } = makeFakeStream({ forceText: true });
  const fake = makeFakeStream();
  const h = makeHarness({ streamFn: fake.streamFn });
  assert.deepEqual(h.lines[0], { type: "ready" });

  h.send(startMessage({ tools: [] }));
  const result = await h.worker.whenFinished;

  assert.equal(result.code, 0);
  const types = h.lines.map((l) => l.type);
  assert.ok(types.includes("delta"));
  assert.equal(types[types.length - 1], "done");
  assert.ok(typeof h.lines.at(-1).elapsed_ms === "number");
  // protocol-only stdout: every line has a known type
  for (const line of h.lines) {
    assert.ok(["ready", "delta", "tool_request", "done", "fatal"].includes(line.type));
  }
  h.stdin.end();
});

test("worker: second start is rejected", async () => {
  const fake = makeFakeStream();
  const h = makeHarness({ streamFn: fake.streamFn });
  h.send(startMessage({ tools: [] }));
  await h.worker.whenFinished;
  h.send(startMessage({ tools: [] }));
  const lines = h.lines;
  const last = lines.at(-1);
  // the worker is finished; a duplicate start cannot produce a second run
  assert.equal(last.type, "done");
  assert.equal(lines.filter((l) => l.type === "done").length, 1);
  h.stdin.end();
});

test("worker: tool_request round-trip then final answer", async () => {
  const fake = makeFakeStream();
  const h = makeHarness({ streamFn: fake.streamFn });
  h.send(startMessage({ max_tool_rounds: 1 }));
  // answer the tool request from Python's side
  const request = await waitForLine(h, (line) => line.type === "tool_request");
  h.send({ type: "tool_result", request_id: request.request_id, ok: true, result: { price: 123.5 } });
  const result = await h.worker.whenFinished;

  assert.equal(result.code, 0);
  const req = h.lines.find((l) => l.type === "tool_request");
  assert.equal(req.name, "get_quote");
  assert.equal(req.tool_call_id, "call_1");
  assert.deepEqual(req.args, { symbol: "AAPL" });
  assert.match(req.request_id, /^[0-9a-f-]{36}$/);
  // final text turn still produced after the tool turn
  assert.equal(fake.calls.length, 2);
  assert.equal(fake.calls[0].toolCount, 1);
  assert.equal(fake.calls[1].toolCount, 0);
  assert.equal(h.lines.at(-1).type, "done");
  h.stdin.end();
});

test("worker: tool failure is surfaced as an error result, not success text", async () => {
  const fake = makeFakeStream();
  const h = makeHarness({ streamFn: fake.streamFn });
  h.send(startMessage({ max_tool_rounds: 1 }));
  const request = await waitForLine(h, (line) => line.type === "tool_request");
  h.send({ type: "tool_result", request_id: request.request_id, ok: false, error: "duckdb: table not found" });
  const result = await h.worker.whenFinished;
  // the model still gets a chance to produce the final answer
  assert.equal(result.code, 0);
  assert.equal(h.lines.at(-1).type, "done");
  h.stdin.end();
});

test("worker: five tool rounds then one tool-free final turn", async () => {
  const fake = makeFakeStream();
  const h = makeHarness({ streamFn: fake.streamFn });
  h.send(startMessage());
  await new Promise((resolve) => {
    const timer = setInterval(() => {
      const pending = h.lines.filter((l) => l.type === "tool_request").length;
      const answered = h.toolResultsSent ?? 0;
      if (pending > answered) {
        const req = h.lines.filter((l) => l.type === "tool_request")[answered];
        h.toolResultsSent = answered + 1;
        h.send({ type: "tool_result", request_id: req.request_id, ok: true, result: "ok" });
      }
      if (h.lines.some((l) => l.type === "done" || l.type === "fatal")) {
        clearInterval(timer);
        resolve();
      }
    }, 5);
  });
  const result = await h.worker.whenFinished;

  assert.equal(result.code, 0);
  assert.equal(fake.calls.length, 6); // 5 tool turns + 1 final answer turn
  assert.equal(fake.calls[4].toolCount, 1); // fifth turn still had tools
  assert.equal(fake.calls[5].toolCount, 0); // sixth turn: tools removed
  assert.equal(h.lines.filter((l) => l.type === "tool_request").length, 5);
  const deltas = h.lines.filter((l) => l.type === "delta").map((l) => l.content).join("");
  assert.equal(deltas, "final answer"); // final text not lost
  assert.equal(h.lines.at(-1).type, "done");
  h.stdin.end();
});

test("worker: provider error is redacted in fatal and stderr", async () => {
  const streamFn = () => {
    const stream = createAssistantMessageEventStream();
    const failing = assistantMessage([{ type: "text", text: "" }], "error");
    failing.errorMessage = `upstream 401 for key ${API_KEY} at /Users/secret/agent/gateway.js`;
    stream.push({ type: "error", reason: "error", error: failing });
    return stream;
  };
  const h = makeHarness({ streamFn });
  h.send(startMessage({ tools: [] }));
  const result = await h.worker.whenFinished;

  assert.equal(result.code, 1);
  const fatal = h.lines.find((l) => l.type === "fatal");
  assert.ok(fatal);
  assert.ok(!fatal.message.includes(API_KEY), "fatal must not contain the api key");
  assert.ok(!fatal.message.includes("/Users/secret"), "fatal must not contain absolute paths");
  const stderrText = h.stderrChunks.join("");
  assert.ok(!stderrText.includes(API_KEY), "stderr must not contain the api key");
  h.stdin.end();
});

test("worker: invalid start messages fail closed without echoing values", async () => {
  const cases = [
    { ...startMessage(), profile: { provider: "anthropic", base_url: "https://x.test", api_key: "k", model: "m" } },
    { ...startMessage(), profile: { provider: "openai_compat", base_url: "https://x.test", model: "m" } },
    { ...startMessage(), max_tool_rounds: 9 },
    { ...startMessage(), tools: [{ name: "x", description: "d", input_schema: {}, read_only: false }] },
  ];
  for (const message of cases) {
    const fake = makeFakeStream();
    const h = makeHarness({ streamFn: fake.streamFn });
    h.send(message);
    const result = await h.worker.whenFinished;
    assert.equal(result.code, 1, `expected fatal for ${JSON.stringify(Object.keys(message))}`);
    const fatal = h.lines.at(-1);
    assert.equal(fatal.type, "fatal");
    assert.ok(!JSON.stringify(h.lines).includes(API_KEY));
    h.stdin.end();
  }
});

test("tool bridge: unknown request_id and malformed results are protocol errors", () => {
  const sent = [];
  const bridge = new ToolBridge((m) => sent.push(m));
  assert.throws(() => bridge.handleToolResult({ type: "tool_result", request_id: "nope", ok: true }), ProtocolError);
  assert.throws(() => bridge.handleToolResult("junk"), ProtocolError);
  const waiting = bridge.requestToolCall({ toolCallId: "c1", name: "t", args: {} });
  const req = sent.find((m) => m.type === "tool_request");
  bridge.handleToolResult({ request_id: req.request_id, ok: true, result: "done" });
  return waiting.then((value) => assert.equal(value, "done"));
});

test("tool bridge: abort rejects the waiting tool", async () => {
  const sent = [];
  const bridge = new ToolBridge((m) => sent.push(m));
  const controller = new AbortController();
  const waiting = bridge.requestToolCall({ toolCallId: "c1", name: "t", args: {} }, controller.signal);
  controller.abort();
  await assert.rejects(waiting, /aborted/);
  // a late result for the aborted request is ignored, not a protocol error
  const req = sent.find((m) => m.type === "tool_request");
  bridge.handleToolResult({ request_id: req.request_id, ok: true, result: "late" });
});

test("makeAgentTools: read-only enforcement and spec field allowlist", () => {
  const bridge = new ToolBridge(() => {});
  assert.throws(
    () => makeAgentTools([{ name: "t", description: "d", input_schema: { type: "object" }, read_only: false }], bridge),
    /read_only/,
  );
  assert.throws(
    () => makeAgentTools([{ name: "t", description: "d", input_schema: { type: "object" }, read_only: true, extra: 1 }], bridge),
    /unknown field/,
  );
  const tools = makeAgentTools(
    [{ name: "t", description: "d", input_schema: { type: "object", properties: { a: { type: "string" } } }, read_only: true }],
    bridge,
  );
  assert.equal(tools[0].executionMode, "sequential");
  assert.equal(tools[0].label, "t");
});
