/** Exercise transport cancellation and failure through dsh's actual subprocess provider. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, writeFileSync, rmSync, readdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {Context} from '@deepseek-ai/cordis';
import LocalSubprocess from '@deepseek-ai/dsh-subprocess-local';
import Tools, {validateJsonSchemaValue} from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import {GitHubWorker, registerGitHub} from '../plugin/github.mjs';

async function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'ascend-github-test-'));
  const path = join(directory, 'worker.mjs');
  writeFileSync(path, `import {createInterface} from 'node:readline';
    const send = value => process.stdout.write(JSON.stringify(value) + '\\n');
    const tasks = new Map();
    send({ready:true,backend:'rest'});
    createInterface({input:process.stdin}).on('line', line => {
      const request = JSON.parse(line);
      if (request.cancel) {
        clearTimeout(tasks.get(request.cancel)); tasks.delete(request.cancel);
        setTimeout(() => send({id:request.cancel,cancelled:true}), 40); return;
      }
      const {id,arguments:args} = request;
      if (args.kind === 'slow') { tasks.set(id,setTimeout(()=>{},60000)); return; }
      if (args.kind === 'crash') process.exit(12);
      if (args.kind === 'invalid') { process.stdout.write('bad json\\n'); return; }
      if (args.kind === 'error') { send({id,failed:true,value:{error:{code:'invalid_argument'}}}); return; }
      send({id,failed:false,value:{results:[{data:args}]}});
    });`);
  const ctx = new Context();
  await ctx.plugin(LocalSubprocess);
  let specification;
  const worker = new GitHubWorker({spawn(spec) {
    specification = spec;
    return ctx.subprocess.spawn({...spec, argv: [process.execPath, path]});
  }}, {python: '/prepared/python', backend: 'rest', env: {}, signal: new AbortController().signal});
  t.after(async () => {
    await worker.dispose();
    await ctx.fiber.dispose();
    rmSync(directory, {recursive: true, force: true});
  });
  await worker.ready;
  return {worker, specification};
}
const signal = () => new AbortController().signal;

test('all backend contracts register in the native dsh schema subset', async () => {
  for (const backend of ['rest', 'graphql', 'dsl']) {
    const ctx = new Context();
    await ctx.plugin(SystemPrompt);
    await ctx.plugin(Tools);
    try {
      registerGitHub(ctx, {backend, ready: Promise.resolve({}), signal: signal()});
      assert.deepEqual(ctx.tools.schemas().map(tool => tool.name), ['github']);
      const schema = ctx.tools.get('github').output.schema;
      const value = backend === 'dsl' ? {data: null} : {results: []};
      assert.deepEqual(validateJsonSchemaValue(schema, value), []);
      assert(validateJsonSchemaValue(schema, {}).length);
    } finally { await ctx.fiber.dispose(); }
  }
});

test('dsh disposal reaps the real Python worker and removes its temporary evidence', async () => {
  const ctx = new Context();
  await ctx.plugin(LocalSubprocess);
  const directory = mkdtempSync(join(tmpdir(), 'ascend-github-cleanup-'));
  const worker = new GitHubWorker(ctx.subprocess, {python: resolve('.venv/bin/python'), backend: 'rest',
    signal: signal(), env: {GH_TOKEN: 'test-only', TMPDIR: directory}});
  try {
    await worker.ready;
    assert.equal(readdirSync(directory).length, 1);
    await assert.rejects(worker.call({}, 'invalid', signal()), /invalid_argument/);
    await worker.dispose();
    assert.deepEqual(readdirSync(directory), []);
  } finally {
    await worker.dispose();
    await ctx.fiber.dispose();
    rmSync(directory, {recursive: true, force: true});
  }
});

test('native worker uses provider argv and returns canonical JSON without an envelope', async t => {
  const {worker, specification} = await fixture(t);
  assert.deepEqual(specification.argv, ['/prepared/python','-I','-u','-m','ascend_github.worker','--backend','rest']);
  const [a, b] = await Promise.all([worker.call({number: 1}, 'a', signal()), worker.call({number: 2}, 'b', signal())]);
  assert.equal(a.results[0].data.number, 1);
  assert.equal(b.results[0].data.number, 2);
  await assert.rejects(worker.call({kind: 'error'}, 'error', signal()), /invalid_argument/);
  assert.equal((await worker.call({}, 'after-error', signal())).results.length, 1);
});

test('cancellation awaits acknowledgement and leaves concurrent calls usable', async t => {
  const {worker} = await fixture(t);
  const control = new AbortController();
  const pending = worker.call({kind: 'slow'}, 'slow', control.signal);
  const rejection = assert.rejects(pending, /cancelled by test/);
  await delay(30);
  const before = Date.now();
  control.abort(new Error('cancelled by test'));
  assert.equal((await worker.call({number: 3}, 'other', signal())).results[0].data.number, 3);
  await rejection;
  assert(Date.now() - before >= 30, 'Returned before the worker acknowledged cancellation');
  assert.equal(worker.pending.size, 0);
});

for (const kind of ['crash', 'invalid']) test(`${kind} fails pending calls after reaping the worker`, async t => {
  const {worker} = await fixture(t);
  const pending = worker.call({kind: 'slow'}, 'slow', signal());
  const failed = assert.rejects(pending);
  await assert.rejects(worker.call({kind}, 'trigger', signal()));
  await failed;
  assert.equal(await worker.handle.waitForExit(AbortSignal.timeout(100)), true);
  assert.equal(worker.pending.size, 0);
});
