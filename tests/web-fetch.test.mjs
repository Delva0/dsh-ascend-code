/** Exercise the real Python web provider through dsh and its durable image store. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {execFileSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {Context} from '@deepseek-ai/cordis';
import LocalSubprocess from '@deepseek-ai/dsh-subprocess-local';
import LocalAttachments from '@deepseek-ai/dsh-attachment-local';
import Llm from '@deepseek-ai/dsh-llm';
import Tools from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import {createScope} from '@deepseek-ai/dsh-scope';
import {registerWebFetch} from '../plugin/web.mjs';

async function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'ascend-web-test-'));
  execFileSync(resolve('.venv/bin/python'), ['-c',
    'from PIL import Image; import random,sys; from pathlib import Path; p=Path(sys.argv[1]); '
    + 'Image.new("RGB",(32,16),"red").save(p/"small.png"); '
    + 'Image.frombytes("RGB",(1800,1800),random.Random(0).randbytes(1800*1800*3)).save(p/"large.png",compress_level=0)',
    directory]);
  const received = [];
  let waiting, disconnected;
  const started = new Promise(resolve => { waiting = resolve; });
  const stopped = new Promise(resolve => { disconnected = resolve; });
  const server = createServer((req, res) => {
    received.push({path: req.url, headers: req.headers});
    if (req.url === '/slow') { waiting(); res.on('close', disconnected); return; }
    if (req.url === '/redirect') { res.writeHead(302, {location: '/text'}).end(); return; }
    if (req.url === '/fail') { res.writeHead(503).end('failed'); return; }
    if (req.url === '/small.png' || req.url === '/large.png') {
      res.writeHead(200, {'content-type': 'image/png'}).end(readFileSync(join(directory, req.url.slice(1))));
      return;
    }
    res.writeHead(200, {'content-type': 'text/plain'}).end('first\nsecond\nthird\n');
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const url = `http://127.0.0.1:${server.address().port}`;
  const ctx = new Context();
  await ctx.plugin(SystemPrompt);
  await ctx.plugin(Tools);
  await ctx.plugin(LocalSubprocess);
  // Local fixture traffic must bypass developer proxies; their upstream
  // connections can outlive a correctly cancelled downstream HTTP request.
  const spawn = ctx.subprocess.spawn.bind(ctx.subprocess);
  ctx.subprocess.spawn = spec => spawn({...spec,
    env: {...spec.env, NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost'}});
  await ctx.plugin(LocalAttachments, {dshHome: directory});
  await ctx.plugin(Llm);
  ctx.llm.resolveModelInfo = async (_provider, model) => ({inputModalities: model === 'vision' ? ['text', 'image'] : ['text']});
  registerWebFetch(ctx, {ready: Promise.resolve({githubPython: resolve('.venv/bin/python')}),
    signal: new AbortController().signal});
  const scopes = [];
  const agent = (model = 'vision') => {
    const caller = {id: randomUUID(), options: {provider: 'test', model: 'text'},
      session: {requestHeader: () => ({config: {provider: 'test', model}})}};
    const scope = createScope(ctx, caller);
    caller.ctx = scope.ctx;
    scopes.push({scope, caller});
    return caller;
  };
  const caller = agent();
  const call = (args, {signal = new AbortController().signal, owner = caller} = {}) => ctx.tools.execute({
    name: 'web_fetch', callId: randomUUID(), arguments: args, agent: owner, signal});
  t.after(async () => {
    for (const {scope, caller} of scopes) {
      await ctx.parallel('agent/disposed', {agent: caller});
      await scope.dispose();
    }
    await ctx.fiber.dispose();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    rmSync(directory, {recursive: true, force: true});
  });
  return {ctx, call, caller, agent, url, received, started, stopped};
}

test('native web_fetch keeps its exact contract, batch failures, redirects and session-local refs', async t => {
  const {ctx, call, caller, agent, url, received} = await fixture(t);
  const spec = JSON.parse(readFileSync('assets/web-fetch.json', 'utf8'));
  assert.deepEqual(ctx.tools.schemas().map(({name, description, parameters}) => ({name, description, parameters})),
    [{name: spec.name, description: spec.description, parameters: spec.parameters}]);
  const result = await call({requests: [17, {url: url + '/redirect', max_lines: 1}, {url: url + '/fail'}]});
  assert.equal(result.isError, false, JSON.stringify(result));
  const [invalid, first, failed] = result.value.results;
  assert(invalid.error && failed.error);
  assert.equal(first.content, 'L1: first');
  const saved = {requests: [{ref: first.ref, start_line: first.next_line}]};
  assert.match((await call(saved)).value.results[0].content, /second.*\n.*third/);
  assert.equal(received.length, 3);
  assert(received.every(request => !request.headers.authorization));
  const foreign = await call(saved, {owner: agent()});
  assert.equal(foreign.isError, true);
  assert.match(JSON.stringify(foreign.content), /Unknown resource ref/);
  assert.equal((await call(saved, {owner: caller})).isError, false);
  assert.equal((await call({requests: [17]})).isError, true);
});

test('native images survive worker disposal, exceed the stdio limit, and follow the routed model', async t => {
  const {ctx, call, caller, url, received} = await fixture(t);
  const result = await call({requests: [{url: url + '/small.png'}, {url: url + '/large.png'}]});
  assert.equal(result.isError, false, JSON.stringify(result));
  assert(result.value.results[1].size > 8 * 1024 * 1024);
  const images = result.content.filter(part => part.type === 'image');
  assert.equal(images.length, 2);
  assert.equal(images[0].attachment.width, 32);
  assert(!JSON.stringify(result).includes('base64,'));
  caller.session.requestHeader = () => ({config: {provider: 'test', model: 'text'}});
  const metadata = await call({requests: [{ref: result.value.results[0].ref}]});
  assert.equal(metadata.isError, false);
  assert.equal(metadata.value.results[0].image_input, false);
  assert(!metadata.content.some(part => part.type === 'image'));
  assert.equal(received.length, 2);
  await ctx.parallel('agent/disposed', {agent: caller});
  for (const image of images) assert((await ctx.attachments.readImage(image.attachment)).data.length);
});

test('cancelling a web download closes it before returning and preserves concurrent results', async t => {
  const {call, url, started, stopped} = await fixture(t);
  const control = new AbortController();
  const slow = call({requests: [{url: url + '/slow'}]}, {signal: control.signal});
  await started;
  const fast = await call({requests: [{url: url + '/text'}]});
  assert.equal(fast.isError, false);
  control.abort(new Error('web cancellation test'));
  assert.equal((await slow).isError, true);
  await Promise.race([stopped, delay(1000).then(() => { throw new Error('Download was not stopped'); })]);
  assert.equal((await call({requests: [{ref: fast.value.results[0].ref}]})).isError, false);
});
