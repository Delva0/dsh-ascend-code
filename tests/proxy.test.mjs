/** Reproduce the launcher's proxy environment, including its IPv6 loopback entries. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {resolve} from 'node:path';
import {Context} from '@deepseek-ai/cordis';
import LocalSubprocess from '@deepseek-ai/dsh-subprocess-local';
import {clearedProxyEnv, installProxyFromEnvironment, proxyEnvironmentForChild} from '@deepseek-ai/dsh-http-proxy';
import {PythonWorker} from '../plugin/worker.mjs';

test('every Python backend starts with dsh proxy policy; web requests retain proxy and bypass routing', async t => {
  const received = [];
  const server = createServer((req, res) => {
    received.push({url: req.url, authorization: req.headers['proxy-authorization']});
    res.writeHead(200, {'content-type': 'text/plain'}).end(req.url.startsWith('http://') ? 'via proxy' : 'direct');
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const names = Object.keys(clearedProxyEnv());
  const previous = Object.fromEntries(names.map(name => [name, process.env[name]]));
  for (const name of names) delete process.env[name];
  process.env.HTTP_PROXY = process.env.HTTPS_PROXY = `http://fixture:password@127.0.0.1:${server.address().port}`;
  const ctx = new Context();
  let restore;
  t.after(async () => {
    await ctx.fiber.dispose();
    await restore?.();
    for (const name of names) {
      if (previous[name] === undefined) delete process.env[name];
      else process.env[name] = previous[name];
    }
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  restore = await installProxyFromEnvironment({get(name) {
    const value = process.env[name];
    return value === undefined ? undefined : {value};
  }}, message => assert.fail(message));
  assert(proxyEnvironmentForChild().NO_PROXY.split(',').includes('[::1]'));
  await ctx.plugin(LocalSubprocess);
  for (const backend of ['rest', 'graphql', 'dsl', 'web_fetch', 'web_search']) {
    await t.test(backend, async () => {
      const worker = new PythonWorker(ctx.subprocess, {
        python: resolve('.venv/bin/python'), backend, signal: new AbortController().signal,
        env: backend === 'web_search' ? {BRAVE_SEARCH_API_KEY: 'test-only'}
          : backend === 'web_fetch' ? {} : {GH_TOKEN: 'test-only'},
        module: backend.startsWith('web_') ? 'ascend_github.web_worker' : 'ascend_github.worker',
      });
      try {
        await worker.ready;
        await assert.rejects(worker.call({}, 'invalid', AbortSignal.timeout(5000)), /invalid_argument/);
        if (backend === 'web_fetch') {
          const reply = await worker.call({requests: [
            {url: `http://127.0.0.1:${server.address().port}/bypass`},
            {url: 'http://upstream.invalid/proxied'},
          ]}, 'routing', AbortSignal.timeout(5000));
          assert.match(reply.results[0].content, /direct/);
          assert.match(reply.results[1].content, /via proxy/);
        }
      } finally { await worker.dispose(); }
    });
  }
  assert.deepEqual(received.find(req => req.url === '/bypass'), {url: '/bypass', authorization: undefined});
  assert.deepEqual(received.find(req => req.url === 'http://upstream.invalid/proxied'), {
    url: 'http://upstream.invalid/proxied', authorization: `Basic ${Buffer.from('fixture:password').toString('base64')}`,
  });
  assert(proxyEnvironmentForChild().NO_PROXY.split(',').includes('[::1]'), 'Parent policy must stay unchanged');
});
