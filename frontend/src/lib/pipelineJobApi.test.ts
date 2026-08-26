import { api } from './api.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

const originalFetch = globalThis.fetch
const calls: string[] = []

try {
  globalThis.fetch = async (input) => {
    calls.push(String(input))
    return new Response(JSON.stringify({ detail: 'job not found' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  const job = await api.pipelineJob('missing-job')

  assert(job === null, 'missing pipeline job must resolve to null')
  assert(calls.length === 1, 'pipeline job lookup must issue exactly one request')
  assert(calls[0] === '/api/pipeline/jobs/missing-job', 'pipeline job lookup path mismatch')
} finally {
  globalThis.fetch = originalFetch
}

console.log('pipelineJobApi.test.ts ok')
