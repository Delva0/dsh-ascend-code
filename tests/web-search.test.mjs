/** Native dsh search transport with the original provider and a local Brave fixture. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {Context} from '@deepseek-ai/cordis';
import LocalSubprocess from '@deepseek-ai/dsh-subprocess-local';
import Tools from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import {createScope} from '@deepseek-ai/dsh-scope';
import {registerWebFetch, registerWebSearch, webSearchEnvironment} from '../plugin/web.mjs';

const secret = 'test-brave-search-key';

test('search settings keep v5 defaults, explicit overrides, and only Brave credentials', () => {
  assert.deepEqual(webSearchEnvironment({}, {}), {DSH_ASCEND_WEB_SEARCH_BACKEND: 'brave',
    DSH_ASCEND_WEB_SEARCH_CONCURRENCY: '1', DSH_ASCEND_WEB_SEARCH_INTERVAL: '2'});
  const env = {BRAVE_SEARCH_API_KEY: secret, BRAVE_API_KEY: 'alias', GH_TOKEN: 'never-forward',
    DSH_ASCEND_WEB_SEARCH_BACKEND: 'auto', DSH_ASCEND_WEB_SEARCH_CONCURRENCY: '3',
    DSH_ASCEND_WEB_SEARCH_INTERVAL: '0.5'};
  assert.deepEqual(webSearchEnvironment({webSearchBackend: 'duckduckgo', webSearchInterval: 0}, env),
    {BRAVE_SEARCH_API_KEY: secret, BRAVE_API_KEY: 'alias', DSH_ASCEND_WEB_SEARCH_BACKEND: 'duckduckgo',
      DSH_ASCEND_WEB_SEARCH_CONCURRENCY: '3', DSH_ASCEND_WEB_SEARCH_INTERVAL: '0'});
  for (const config of [{webSearchBackend: 'other'}, {webSearchConcurrency: 0}, {webSearchConcurrency: 1.5},
    {webSearchInterval: -1}, {webSearchInterval: NaN}, {webSearchInterval: Infinity}]) {
    assert.throws(() => webSearchEnvironment(config, {}));
  }
});

async function fixture(t, {key = true, interval = 0, concurrency = 1} = {}) {
  const ctx = new Context(), scopes = [], received = [], launches = [];
  const credentialNames = ['BRAVE_SEARCH_API_KEY', 'BRAVE_API_KEY', 'GH_TOKEN'];
  const previous = Object.fromEntries(credentialNames.map(name => [name, process.env[name]]));
  delete process.env.BRAVE_API_KEY;
  if (key) process.env.BRAVE_SEARCH_API_KEY = secret;
  else delete process.env.BRAVE_SEARCH_API_KEY;
  process.env.GH_TOKEN = 'test-github-not-for-web';
  let enterSlow, closeSlow;
  const started = new Promise(resolve => { enterSlow = resolve; });
  const stopped = new Promise(resolve => { closeSlow = resolve; });
  const server = createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    received.push({url, headers: req.headers, time: Date.now()});
    if (url.pathname === '/search') {
      const query = url.searchParams.get('q');
      if (query === 'slow') { enterSlow(); res.on('close', closeSlow); return; }
      if (query === 'limited') {
        res.writeHead(429, {'content-type': 'application/json', 'retry-after': '60'});
        res.end(JSON.stringify({error: {code: 'RATE_LIMITED'}}));
        return;
      }
      res.writeHead(200, {'content-type': 'application/json'});
      res.end(JSON.stringify({type: 'search', query: {original: query, more_results_available: true},
        web: {results: [{title: 'Evidence', url: `http://127.0.0.1:${server.address().port}/evidence`,
          description: 'Summary', extra_snippets: ['Detail']}]}}));
      return;
    }
    res.writeHead(200, {'content-type': 'text/plain'}).end('Verified source');
  });
  t.after(async () => {
    for (const {scope, caller} of scopes) {
      await ctx.parallel('agent/disposed', {agent: caller});
      await scope.dispose();
    }
    await ctx.fiber.dispose();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    for (const name of credentialNames) {
      if (previous[name] === undefined) delete process.env[name];
      else process.env[name] = previous[name];
    }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  await ctx.plugin(SystemPrompt);
  await ctx.plugin(Tools);
  await ctx.plugin(LocalSubprocess);
  const spawn = ctx.subprocess.spawn.bind(ctx.subprocess);
  ctx.subprocess.spawn = spec => {
    launches.push(spec);
    assert(!spec.argv.join(' ').includes(secret));
    const tool = spec.argv.at(-1);
    const script = 'import os; import ascend_github.web_worker as worker; '
      + 'import ascend_github.core.tool_web as web; '
      + `web.BRAVE_SEARCH_URL="http://127.0.0.1:${server.address().port}/search"; `
      + 'assert not os.environ.get("GH_TOKEN"); '
      + (tool === 'web_fetch' ? 'assert not os.environ.get("BRAVE_SEARCH_API_KEY"); ' : '')
      + 'worker.main()';
    return spawn({...spec, argv: [spec.argv[0], '-I', '-u', '-c', script, '--backend', tool],
      env: {...spec.env, NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost'}});
  };
  const options = {ready: Promise.resolve({githubPython: resolve('.venv/bin/python')}),
    signal: new AbortController().signal};
  registerWebFetch(ctx, options);
  registerWebSearch(ctx, {...options, config: {webSearchBackend: 'brave',
    webSearchInterval: interval, webSearchConcurrency: concurrency}});
  const agent = () => {
    const caller = {id: randomUUID()};
    const scope = createScope(ctx, caller);
    caller.ctx = scope.ctx;
    scopes.push({scope, caller});
    return caller;
  };
  const caller = agent();
  const call = (name, args, {signal = new AbortController().signal, owner = caller} = {}) => ctx.tools.execute({
    name, callId: randomUUID(), arguments: args, agent: owner, signal});
  return {ctx, call, agent, received, launches, started, stopped,
    url: `http://127.0.0.1:${server.address().port}`};
}

test('native search preserves the contract, batch errors, cross-call pacing and search-to-fetch credentials', async t => {
  const {ctx, call, received, launches} = await fixture(t, {interval: 0.15});
  const spec = JSON.parse(readFileSync('assets/web-search.json', 'utf8'));
  const actual = ctx.tools.schemas().find(tool => tool.name === 'web_search');
  assert.deepEqual(actual, {name: spec.name, description: spec.description, parameters: spec.parameters});
  assert.equal((await call('web_search', {queries: [{query: 'warm-up'}]})).isError, false);
  received.length = 0;
  const args = {queries: [17, {query: 'topic', max_results: 7, page: 3, region: 'cn-zh', timelimit: 'm'}]};
  const started = Date.now();
  const [one, two] = await Promise.all([call('web_search', args),
    call('web_search', {queries: [{query: 'later'}]})]);
  assert.equal(one.isError, false, JSON.stringify(one));
  assert.equal(two.isError, false, JSON.stringify(two));
  assert(one.value.results[0].error);
  const found = one.value.results[1];
  assert.equal(found.backend, 'brave');
  assert.equal(found.items[0].title, 'Evidence');
  assert.equal(ctx.tools.get('web_search').presentCall(args).kind, 'search');
  assert.equal(received.length, 2);
  assert(Date.now() - started >= 100, 'Concurrent calls bypassed their shared search interval');
  const params = received.find(item => item.url.searchParams.get('q') === 'topic').url.searchParams;
  assert.equal(params.get('count'), '7');
  assert.equal(params.get('offset'), '2');
  assert.equal(params.get('freshness'), 'pm');
  const fetched = await call('web_fetch', {requests: [{url: found.items[0].href}]});
  assert.equal(fetched.isError, false, JSON.stringify(fetched));
  assert.match(fetched.value.results[0].content, /Verified source/);
  assert(received.slice(0, 2).every(item => item.headers['x-subscription-token'] === secret));
  assert(!received[2].headers['x-subscription-token'] && !received[2].headers.authorization);
  assert(!JSON.stringify([one, two, fetched]).includes(secret));
  assert.equal(launches.filter(item => item.argv.at(-1) === 'web_search').length, 1);
  assert.equal((await call('web_search', {queries: [17]})).isError, true);
});

test('a rate limit is shared across search calls but remains isolated from other sessions', async t => {
  const {call, agent, received} = await fixture(t);
  const limited = await call('web_search', {queries: [{query: 'limited'}, {query: 'queued'}]});
  assert.equal(limited.isError, true);
  assert.match(JSON.stringify(limited.content), /RATE_LIMITED/);
  const cooled = await call('web_search', {queries: [{query: 'later'}]});
  assert.equal(cooled.isError, true);
  assert.match(JSON.stringify(cooled.content), /no request sent/);
  assert.equal(received.length, 1);
  assert.equal((await call('web_search', {queries: [{query: 'other'}]}, {owner: agent()})).isError, false);
  assert.equal(received.length, 2);
});

test('missing Brave credentials fail search without blocking web_fetch', async t => {
  const {call, url, received} = await fixture(t, {key: false});
  const search = await call('web_search', {queries: [{query: 'topic'}]});
  assert.equal(search.isError, true);
  assert.match(JSON.stringify(search.content), /BRAVE_SEARCH_API_KEY/);
  assert.equal(received.length, 0);
  assert.equal((await call('web_fetch', {requests: [{url: url + '/evidence'}]})).isError, false);
});

test('search cancellation settles the HTTP request and leaves other searches usable', async t => {
  const {call, started, stopped} = await fixture(t, {concurrency: 2});
  const control = new AbortController();
  const pending = call('web_search', {queries: [{query: 'slow'}]}, {signal: control.signal});
  await started;
  assert.equal((await call('web_search', {queries: [{query: 'fast'}]})).isError, false);
  control.abort(new Error('search cancelled by test'));
  assert.equal((await pending).isError, true);
  await Promise.race([stopped, delay(1000).then(() => { throw new Error('Search request was not stopped'); })]);
  assert.equal((await call('web_search', {queries: [{query: 'after-cancel'}]})).isError, false);
});
