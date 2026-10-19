/** Provider-managed JSON-lines workers, scoped to each dsh agent. */
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const maxFrameBytes = 8 * 1024 * 1024;

function until(promise, signal) {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener('abort', abort, {once: true});
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}

export class PythonWorker {
  constructor(subprocess, {python, backend, signal, env, module = 'ascend_github.worker', label = 'GitHub'}) {
    this.label = label;
    this.pending = new Map();
    this.buffer = '';
    this.ready = new Promise((resolve, reject) => {
      this.resolveReady = resolve;
      this.rejectReady = reject;
    });
    this.ready.catch(() => {});
    this.handle = subprocess.spawn({
      argv: [python, '-I', '-u', '-m', module, '--backend', backend],
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
          if (Buffer.byteLength(line) > maxFrameBytes) throw new Error(`${this.label} response frame is too large`);
          const reply = JSON.parse(line);
          if (reply.ready === true) {
            if (reply.backend !== backend) throw new Error(`${this.label} worker backend mismatch`);
            this.resolveReady();
            continue;
          }
          const pending = this.pending.get(reply.id);
          if (!pending) throw new Error(`Unknown ${this.label} response id`);
          if (reply.cancelled !== true && (typeof reply.failed !== 'boolean'
              || !reply.value || typeof reply.value !== 'object' || Array.isArray(reply.value))) {
            throw new Error(`Invalid ${this.label} response`);
          }
          this.pending.delete(reply.id);
          pending.finish(reply);
        }
        if (Buffer.byteLength(this.buffer) > maxFrameBytes) throw new Error(`${this.label} response frame is too large`);
      } catch (error) { this.fail(error); }
    });
    this.done = this.handle.done.then(outcome => {
      const detail = this.handle.collected.stderr?.readFrom(0).text.trim();
      this.fail(new Error(`${this.label} worker exited (${outcome.signal || outcome.exitCode})${detail ? `: ${detail}` : ''}`));
    }, error => this.fail(error));
    const timer = setTimeout(() => this.fail(new Error(`${this.label} worker startup timed out`)), 20000);
    this.ready.finally(() => clearTimeout(timer)).catch(() => {});
  }

  fail(error) {
    if (this.error) return;
    this.error = error;
    this.rejectReady(error);
    this.handle.terminate();
    // Drain the managed process before any in-flight call reports failure.
    void this.handle.waitForExit(AbortSignal.timeout(5000)).then(exited => {
      const failure = exited ? error : new Error(`${this.label} worker did not terminate: ${error.message}`);
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
    if (Buffer.byteLength(line) > maxFrameBytes) throw new Error(`${this.label} request frame is too large`);
    this.handle.stdin.write(line, error => { if (error) this.fail(error); });
  }

  async request(args, callId, signal, context = {}) {
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
        timer = setTimeout(() => this.fail(new Error(`${this.label} cancellation timed out`)), 5000);
        try { this.write({cancel: id}); } catch (error) { this.fail(error); }
      };
      this.pending.set(id, {
        finish: reply => {
          cleanup();
          if (signal.aborted) reject(signal.reason);
          else if (reply.cancelled) reject(new Error(`${this.label} call cancelled`));
          else if (reply.failed) reject(new Error(JSON.stringify(reply.value)));
          else resolve(reply);
        },
        fail: error => { cleanup(); reject(signal.aborted ? signal.reason : error); },
      });
      signal.addEventListener('abort', abort, {once: true});
      try { this.write({id, call_id: callId, arguments: args, context}); }
      catch (error) {
        this.pending.delete(id);
        cleanup();
        reject(error);
      }
    });
  }

  async call(args, callId, signal) {
    return (await this.request(args, callId, signal)).value;
  }

  async dispose() {
    this.fail(new Error(`${this.label} worker disposed`));
    if (!await this.handle.waitForExit(AbortSignal.timeout(5000))) throw new Error(`${this.label} worker did not stop`);
    await this.done;
  }
}

export function createWorkerPool(ctx, {backend, ready, signal, env = {},
  module = 'ascend_github.worker', label = 'GitHub'}) {
  const workers = new Map();
  const closing = new Set();
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
    close(agent.id)?.catch(error => ctx.logger.warn(`${label} worker cleanup: ${error.message}`));
  });
  ctx.effect(() => async () => {
    for (const key of workers.keys()) close(key);
    await Promise.all([...closing]);
  });
  return async (args, exec, context) => {
    const operation = AbortSignal.any([exec.signal, signal]);
    const {githubPython} = await until(ready, operation);
    operation.throwIfAborted();
    const key = exec.agent?.id ?? 'operator';
    let worker = workers.get(key);
    while (worker?.error) { await close(key); worker = workers.get(key); }
    operation.throwIfAborted();
    if (!worker) {
      worker = new PythonWorker(ctx.subprocess, {python: githubPython, backend, signal, env, module, label});
      workers.set(key, worker);
    }
    return worker.request(args, exec.callId, operation, context);
  };
}
