/** Native dsh GitHub tool backed by a provider-managed Python worker per agent. */
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const maxFrameBytes = 8 * 1024 * 1024;
const text = value => [{type: 'text', text: JSON.stringify(value)}];

function until(promise, signal) {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener('abort', abort, {once: true});
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}

export class GitHubWorker {
  constructor(subprocess, {python, backend, signal, env}) {
    this.pending = new Map();
    this.buffer = '';
    this.ready = new Promise((resolve, reject) => {
      this.resolveReady = resolve;
      this.rejectReady = reject;
    });
    this.ready.catch(() => {});
    this.handle = subprocess.spawn({
      argv: [python, '-I', '-u', '-m', 'ascend_github.worker', '--backend', backend],
      cwd: root, env,
      stdio: {stdin: 'pipe', stdout: 'pipe', stderr: {maxBytes: 16384}},
      graceMs: 1000, signal,
    });
    this.handle.stdin.on('error', error => this.fail(error));
    this.handle.stdout.setEncoding('utf8');
    this.handle.stdout.on('error', error => this.fail(error));
    this.handle.stdout.on('data', chunk => {
      try {
        this.buffer += chunk;
        let newline;
        while ((newline = this.buffer.indexOf('\n')) !== -1) {
          const line = this.buffer.slice(0, newline);
          this.buffer = this.buffer.slice(newline + 1);
          if (Buffer.byteLength(line) > maxFrameBytes) throw new Error('GitHub response frame is too large');
          const reply = JSON.parse(line);
          if (reply.ready === true) {
            if (reply.backend !== backend) throw new Error('GitHub worker backend mismatch');
            this.resolveReady();
            continue;
          }
          const pending = this.pending.get(reply.id);
          if (!pending) throw new Error('Unknown GitHub response id');
          if (reply.cancelled !== true && (typeof reply.failed !== 'boolean'
              || !reply.value || typeof reply.value !== 'object' || Array.isArray(reply.value))) {
            throw new Error('Invalid GitHub response');
          }
          this.pending.delete(reply.id);
          pending.finish(reply);
        }
        if (Buffer.byteLength(this.buffer) > maxFrameBytes) throw new Error('GitHub response frame is too large');
      } catch (error) { this.fail(error); }
    });
    this.done = this.handle.done.then(outcome => {
      const detail = this.handle.collected.stderr?.readFrom(0).text.trim();
      this.fail(new Error(`GitHub worker exited (${outcome.signal || outcome.exitCode})${detail ? `: ${detail}` : ''}`));
    }, error => this.fail(error));
    const timer = setTimeout(() => this.fail(new Error('GitHub worker startup timed out')), 20000);
    this.ready.finally(() => clearTimeout(timer)).catch(() => {});
  }

  fail(error) {
    if (this.error) return;
    this.error = error;
    this.rejectReady(error);
    this.handle.terminate();
    // Drain the managed process before any in-flight call reports failure.
    void this.handle.waitForExit(AbortSignal.timeout(5000)).then(exited => {
      const failure = exited ? error : new Error(`GitHub worker did not terminate: ${error.message}`);
      for (const pending of this.pending.values()) pending.fail(failure);
      this.pending.clear();
    }, failure => {
      for (const pending of this.pending.values()) pending.fail(failure);
      this.pending.clear();
    });
  }

  write(message) {
    if (this.error) throw this.error;
    const line = JSON.stringify(message) + '\n';
    if (Buffer.byteLength(line) > maxFrameBytes) throw new Error('GitHub request frame is too large');
    this.handle.stdin.write(line, error => { if (error) this.fail(error); });
  }

  async call(args, callId, signal) {
    await until(this.ready, signal);
    signal.throwIfAborted();
    if (this.error) throw this.error;
    const id = randomUUID();
    return new Promise((resolve, reject) => {
      let timer;
      const cleanup = () => {
        clearTimeout(timer);
        signal.removeEventListener('abort', abort);
      };
      const abort = () => {
        // A cancellation reply is sent only after the Python task has settled.
        timer = setTimeout(() => this.fail(new Error('GitHub cancellation timed out')), 5000);
        try { this.write({cancel: id}); } catch (error) { this.fail(error); }
      };
      this.pending.set(id, {
        finish: reply => {
          cleanup();
          if (signal.aborted) reject(signal.reason);
          else if (reply.cancelled) reject(new Error('GitHub call cancelled'));
          else if (reply.failed) reject(new Error(JSON.stringify(reply.value)));
          else resolve(reply.value);
        },
        fail: error => { cleanup(); reject(signal.aborted ? signal.reason : error); },
      });
      signal.addEventListener('abort', abort, {once: true});
      try { this.write({id, call_id: callId, arguments: args}); }
      catch (error) {
        this.pending.delete(id);
        cleanup();
        reject(error);
      }
    });
  }

  async dispose() {
    this.fail(new Error('GitHub worker disposed'));
    if (!await this.handle.waitForExit(AbortSignal.timeout(5000))) throw new Error('GitHub worker did not stop');
    await this.done;
  }
}

export function registerGitHub(ctx, {backend, ready, signal}) {
  const spec = JSON.parse(readFileSync(new URL(`../assets/github-${backend}.json`, import.meta.url), 'utf8'));
  // dsh's schema subset uses oneOf for nullable values. Error-only DSL results
  // throw before output validation; every successful DSL value has data.
  const outputSchema = backend === 'dsl' ? {type: 'object', required: ['data'], properties: {
    ...spec.returns.properties, data: {oneOf: [{type: 'object'}, {type: 'null'}]},
  }} : spec.returns;
  const workers = new Map();
  const closing = new Set();
  const env = Object.fromEntries(['GH_TOKEN', 'GITHUB_TOKEN', 'GITHUB_PERSONAL_ACCESS_TOKEN']
    .filter(key => process.env[key]).map(key => [key, process.env[key]]));
  const close = key => {
    const worker = workers.get(key);
    workers.delete(key);
    if (!worker) return;
    const pending = worker.dispose();
    closing.add(pending);
    pending.then(() => closing.delete(pending), () => closing.delete(pending));
    return pending;
  };
  // agent/disposed is a synchronous emit event. Own its asynchronous teardown
  // explicitly so errors are observed and plugin unload also joins these workers.
  ctx.on('agent/disposed', ({agent}) => {
    close(agent.id)?.catch(error => ctx.logger.warn(`GitHub worker cleanup: ${error.message}`));
  });
  ctx.effect(() => async () => {
    for (const key of workers.keys()) close(key);
    await Promise.all([...closing]);
  });
  ctx.tools.register({
    name: 'github', description: spec.description, parameters: spec.parameters,
    isConcurrencySafe: () => true, timeoutMs: 180000,
    output: {schema: outputSchema, render: (_args, value) => text(value)},
    presentCall(args) {
      const requests = Array.isArray(args?.requests) ? args.requests : [];
      const label = requests.length === 1 ? requests[0]?.path : undefined;
      return {card: 'generic', kind: 'fetch', title: label ? `GitHub ${label}` : 'GitHub',
        rawInput: requests.length ? requests : args?.query};
    },
    async execute(args, exec) {
      const operation = AbortSignal.any([exec.signal, signal]);
      const {githubPython} = await until(ready, operation);
      operation.throwIfAborted();
      const key = exec.agent?.id ?? 'operator';
      let worker = workers.get(key);
      while (worker?.error) { await close(key); worker = workers.get(key); }
      operation.throwIfAborted();
      if (!worker) {
        worker = new GitHubWorker(ctx.subprocess, {python: githubPython, backend, signal, env});
        workers.set(key, worker);
      }
      // The Python registry validates batch members individually, preserving valid siblings.
      return worker.call(args, exec.callId, operation);
    },
  });
}
