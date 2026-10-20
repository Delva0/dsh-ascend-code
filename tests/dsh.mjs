/** Real dsh registry/preset/transport integration, without invoking a model. */
import assert from 'node:assert/strict';
import {mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {boot, loadOverlayPatches} from '@deepseek-ai/dsh-app-boot';
import {createScope} from '@deepseek-ai/dsh-scope';
import {renderPrompt} from '@deepseek-ai/dsh-system-prompt';
import {installProxyFromEnvironment} from '@deepseek-ai/dsh-http-proxy';

mkdirSync('build', {recursive: true});
const fixture = mkdtempSync(resolve('build/dsh-test-'));
mkdirSync(join(fixture, 'node_modules'), {recursive: true});
symlinkSync(process.env.DSH_ASCEND_PLUGIN_ROOT || resolve('.'), join(fixture, 'node_modules/dsh-ascend-code'), 'dir');
const mock = join(fixture, 'host.mjs');
writeFileSync(mock, `export const inject = ['tools']; export function apply(ctx) {
 ctx.tools.register({name:'bash',description:'Host fixture',parameters:{type:'object',properties:{}},
 output:{schema:{},render:()=>[{type:'text',text:'host'}]},execute:async()=>({content:'host'})});
}`);
const base = join(fixture, 'cordis.yml');
writeFileSync(base, JSON.stringify([
  {id: 'prompt', name: '@deepseek-ai/dsh-system-prompt'},
  {id: 'tools', name: '@deepseek-ai/dsh-tools'},
  {id: 'subprocess', name: '@deepseek-ai/dsh-subprocess-local'},
  {id: 'attachments', name: '@deepseek-ai/dsh-attachment-local', config: {dshHome: fixture}},
  {id: 'llm', name: '@deepseek-ai/dsh-llm'},
  {id: 'projection', name: '@deepseek-ai/dsh-session-projection'},
  {id: 'registry', name: '@deepseek-ai/dsh-agent-preset-registry', config: {default: 'standard'}},
  {id: 'host', name: mock},
  {id: 'standard', name: '@deepseek-ai/dsh-agent-preset', config: {id: 'standard', plugins: []}},
]));
// boot() alone skips the CLI launcher's proxy installation. Exercise the same
// inherited environment as the actual Web app, including bracketed IPv6 bypass.
const restoreProxy = await installProxyFromEnvironment({get(name) {
  const value = process.env[name];
  return value === undefined ? undefined : {value};
}}, message => console.warn(message));
const ctx = await boot('ascend-test', base, loadOverlayPatches('ascend-test', resolve('cordis.patch.yml')));
const notices = [];
const agent = {id: randomUUID(), steer(message) { notices.push(message); }};
const scope = createScope(ctx, agent);
agent.ctx = scope.ctx;
async function call(name, args, signal = new AbortController().signal) {
  return ctx.tools.execute({agent, callId: randomUUID(), name, arguments: args, signal});
}
const value = result => {
  assert.equal(result.isError, false, JSON.stringify(result));
  return result.value;
};
try {
  const roster = await ctx.agentPresets.list();
  assert.equal(roster.find(p => p.id === 'ascend')?.broken, undefined, JSON.stringify(roster));
  await ctx.agentPresets.mount(agent.ctx, 'standard');
  assert.equal(value(await call('bash', {})).content, 'host');
  await ctx.agentPresets.recompose(agent.ctx, 'ascend');
  const prompt = renderPrompt(await ctx.systemPrompt.assemble({scope: agent}));
  console.log('Prepared Ascend workspace and assembled its prompt');
  assert.deepEqual(ctx.tools.schemas(agent).map(t => t.name).sort(), ['bash', 'github', 'web_fetch', 'web_search']);
  ctx.tools.register({name: 'late_host_tool', description: 'Must stay on the host preset',
    parameters: {type: 'object', properties: {}}, output: {schema: {}, render: () => []},
    execute: async () => ({host: true})});
  assert.deepEqual(ctx.tools.schemas(agent).map(t => t.name).sort(), ['bash', 'github', 'web_fetch', 'web_search']);
  assert.equal((await call('late_host_tool', {})).isError, true);
  assert.equal(ctx.tools.get('bash', agent).description, readFileSync('prompts/bash.md', 'utf8'));
  assert.equal(ctx.tools.get('bash').description, 'Host fixture');
  assert(prompt.includes('Always use `lsp` to read Python code'));
  assert(prompt.includes('/workspace/vllm-ascend'));
  assert(!prompt.includes('{{WORKDIR}}'));
  const probe = value(await call('bash', {command: 'pwd; lsp --version; git -C /workspace/vllm rev-parse --is-shallow-repository'}));
  assert.match(probe.output, /\/workspace\nlsp 0\.18\.18\nfalse/);
  const read = value(await call('bash', {command: "cd /workspace/vllm && lsp show 'HEAD:LLM' --roles doc"}));
  assert.match(read.output, /class LLM/);
  assert.equal(value(await call('bash', {command: 'pwd'})).output.trim(), '/workspace');
  assert.equal((await call('bash', {command: 'true', workdir: '/'})).isError, true);
  assert.equal((await call('bash', {command: 'exit 7'})).isError, true);
  const backend = process.env.DSH_ASCEND_GITHUB_BACKEND || 'rest';
  const githubArgs = backend === 'rest' ? {
    requests: [{path: '/repos/vllm-project/vllm-ascend', fields: {name: '/full_name'}}],
  } : backend === 'graphql' ? {
    requests: [{query: '{repository(owner:"vllm-project", name:"vllm-ascend") {nameWithOwner}}'}],
  } : {query: '{repository(owner:"vllm-project", name:"vllm-ascend") {nameWithOwner}}'};
  const github = value(await call('github', githubArgs));
  assert.match(JSON.stringify(github), /vllm-project\/vllm-ascend/);
  assert(!('structuredContent' in github), 'Native tool must return its canonical backend value');
  assert.equal(ctx.tools.get('github', agent).presentCall(githubArgs).kind, 'fetch');
  assert.equal((await call('github', {})).isError, true);
  const fetched = value(await call('web_fetch', {requests: [{
    url: 'https://raw.githubusercontent.com/vllm-project/vllm-ascend/main/README.md', max_lines: 3,
  }]})).results[0];
  assert.equal(fetched.status, 200);
  const continued = value(await call('web_fetch', {requests: [{ref: fetched.ref,
    start_line: fetched.next_line, max_lines: 3}]})).results[0];
  assert.equal(continued.ref, fetched.ref);
  assert.equal(continued.start_line, fetched.next_line);
  const searchArgs = {queries: [{query: 'site:github.com/vllm-project/vllm-ascend vllm ascend', max_results: 3}]};
  assert.equal(ctx.tools.get('web_search', agent).presentCall(searchArgs).kind, 'search');
  if ((process.env.DSH_ASCEND_WEB_SEARCH_BACKEND || 'brave') === 'brave'
      && ![process.env.BRAVE_SEARCH_API_KEY, process.env.BRAVE_API_KEY].some(key => key?.trim())) {
    const missing = await call('web_search', searchArgs);
    assert.equal(missing.isError, true);
    assert.match(JSON.stringify(missing.content), /BRAVE_SEARCH_API_KEY/);
    console.log('PASS: missing Brave credentials are explicit; web_fetch remains available');
  } else {
    const searched = value(await call('web_search', searchArgs)).results[0];
    assert(searched.returned_count > 0, JSON.stringify(searched));
    assert(searched.items.every(item => /^https?:\/\//.test(item.href)));
    console.log(`PASS: live native web_search (${searched.backend}) returned ${searched.returned_count} results`);
  }
  if (backend === 'rest') {
    const issues = value(await call('github', {requests: [{
      path: '/search/issues',
      params: {q: 'repo:vllm-project/vllm-ascend v0.30 bug', sort: 'created', order: 'desc', per_page: 20},
      fields: {n: '/number', t: '/title', s: '/state', c: '/created_at', u: '/html_url'},
    }]})).results[0];
    assert.equal(issues.status, 200, JSON.stringify(issues));
    assert(!issues.error);
    console.log('PASS: reported GitHub issue-search request with the real dsh proxy policy');
    const saved = {requests: [{result_id: github.results[0].result_id, fields: {name: '/full_name'}}]};
    assert.equal(value(await call('github', saved)).results[0].data.name, 'vllm-project/vllm-ascend');
    const partial = value(await call('github', {requests: [17, ...saved.requests]}));
    assert(partial.results[0].error);
    assert.equal(partial.results[1].data.name, 'vllm-project/vllm-ascend');
    assert.equal((await call('github', {requests: [17]})).isError, true);
    const other = {id: randomUUID(), steer() {}};
    const otherScope = createScope(ctx, other);
    other.ctx = otherScope.ctx;
    try {
      await ctx.agentPresets.mount(other.ctx, 'ascend');
      const foreign = await ctx.tools.execute({agent: other, callId: randomUUID(), name: 'github',
        arguments: saved, signal: new AbortController().signal});
      assert.equal(foreign.isError, true, 'GitHub evidence must stay in its owning session');
      assert.match(JSON.stringify(foreign.content), /Unknown result_id/);
    } finally {
      await ctx.parallel('agent/disposed', {agent: other});
      await otherScope.dispose();
    }
    assert.equal(value(await call('github', saved)).results[0].data.name, 'vllm-project/vllm-ascend');
  }
  const background = value(await call('bash', {command: 'sleep 0.4; printf background-ok', run_in_background: true}));
  assert(background.task_id);
  for (let i = 0; i < 100 && !notices.length; i++) await delay(100);
  assert.equal(notices.length, 1, 'Background completion notification was not delivered');
  assert.match(JSON.stringify(notices[0]), /background-ok/);
  const control = new AbortController();
  const marker = `/tmp/ascend-cancel-${randomUUID()}`;
  const pending = call('bash', {command: `sleep 3; touch ${marker}`}, control.signal);
  await delay(500);
  control.abort(new Error('Integration cancellation'));
  assert.equal((await pending).isError, true);
  await delay(3100);
  value(await call('bash', {command: `test ! -e ${marker}`}));
  await ctx.agentPresets.recompose(agent.ctx, 'standard');
  assert.equal(value(await call('bash', {})).content, 'host');
  assert(!ctx.tools.schemas(agent).some(t => t.name === 'github'));
  assert(!ctx.tools.schemas(agent).some(t => t.name === 'web_fetch'));
  assert(!ctx.tools.schemas(agent).some(t => t.name === 'web_search'));
  console.log(`PASS: native bash/GitHub (${backend})/web_fetch/web_search, prompt, preset isolation/switch, real Docker/lsp, background notification, cancellation`);
} finally {
  await scope.dispose();
  await ctx.fiber.dispose();
  await restoreProxy();
  rmSync(fixture, {recursive: true, force: true});
}
