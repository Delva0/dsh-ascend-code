/** Real dsh registry/preset/transport integration, without invoking a model. */
import assert from 'node:assert/strict';
import {mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {boot, loadOverlayPatches} from '@deepseek-ai/dsh-app-boot';
import {createScope} from '@deepseek-ai/dsh-scope';
import {renderPrompt} from '@deepseek-ai/dsh-system-prompt';

mkdirSync('build', {recursive: true});
const fixture = mkdtempSync(resolve('build/dsh-test-'));
mkdirSync(join(fixture, 'node_modules/@delva'), {recursive: true});
symlinkSync(process.env.DSH_ASCEND_PLUGIN_ROOT || resolve('.'), join(fixture, 'node_modules/@delva/dsh-ascend'), 'dir');
const mock = join(fixture, 'host.mjs');
writeFileSync(mock, `export const inject = ['tools']; export function apply(ctx) {
 ctx.tools.register({name:'bash',description:'Host fixture',parameters:{type:'object',properties:{}},
 output:{schema:{},render:()=>[{type:'text',text:'host'}]},execute:async()=>({content:'host'})});
}`);
const base = join(fixture, 'cordis.yml');
writeFileSync(base, JSON.stringify([
  {id: 'prompt', name: '@deepseek-ai/dsh-system-prompt'},
  {id: 'tools', name: '@deepseek-ai/dsh-tools'},
  {id: 'projection', name: '@deepseek-ai/dsh-session-projection'},
  {id: 'registry', name: '@deepseek-ai/dsh-agent-preset-registry', config: {default: 'standard'}},
  {id: 'host', name: mock},
  {id: 'standard', name: '@deepseek-ai/dsh-agent-preset', config: {id: 'standard', plugins: []}},
]));
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
  assert.deepEqual(ctx.tools.schemas(agent).map(t => t.name).sort(), ['bash', 'mcp__ascend_github__github']);
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
  const github = value(await call('mcp__ascend_github__github', {
    requests: [{path: '/repos/vllm-project/vllm-ascend', fields: {name: '/full_name'}}],
  }));
  assert.equal(github.structuredContent.results[0].data.name, 'vllm-project/vllm-ascend');
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
  assert(!ctx.tools.schemas(agent).some(t => t.name === 'mcp__ascend_github__github'));
  console.log('PASS: native bash, prompt, preset isolation/switch, real Docker/lsp/GitHub, background notification, cancellation');
} finally {
  await scope.dispose();
  await ctx.fiber.dispose();
  rmSync(fixture, {recursive: true, force: true});
}
