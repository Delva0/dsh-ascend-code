/** Preset-scoped native bash, v5 prompts, and durable container tasks. */
import {existsSync, readFileSync} from 'node:fs';
import {homedir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import z from '@deepseek-ai/schemastery';
import {createUserMessage} from '@deepseek-ai/dsh-llm';
import {scrubbedParentEnv} from '@deepseek-ai/dsh-subprocess';
import {PERSONA_PREFIX_SECTION} from '@deepseek-ai/dsh-system-prompt';
import {validateJsonSchemaValue} from '@deepseek-ai/dsh-tools';
import * as mcp from '@deepseek-ai/dsh-mcp-client';
import {Docker} from './docker.mjs';

export const name = 'dsh-ascend';
export const inject = ['tools', 'systemPrompt'];
export const Config = z.object({
  container: z.string(),
  githubCommand: z.string(),
  githubBackend: z.union(['rest', 'graphql', 'dsl']),
  workdir: z.string().default('/workspace'),
  maxOutputBytes: z.number().default(100000),
});
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
export const bashDescription = read('../prompts/bash.md');
export const bashSchema = JSON.parse(read('../assets/bash-schema.json'));
const terminal = state => ['completed', 'stopped', 'timed_out', 'interrupted', 'failed'].includes(state.status);

export function normalizeBash(args) {
  if (args && typeof args === 'object' && !Array.isArray(args)) {
    args = {...args};
    if (typeof args.timeout === 'string' && /^-?\d+(\.\d+)?$/.test(args.timeout)) args.timeout = Number(args.timeout);
    if (args.run_in_background === 'true' || args.run_in_background === 'false') {
      args.run_in_background = args.run_in_background === 'true';
    }
  }
  const violations = validateJsonSchemaValue(bashSchema, args);
  if (violations.length) throw new Error(violations.join('; '));
  return args;
}

export function renderPrompt(info, maximum) {
  const values = {WORKDIR: info.workdir, BASH_MAX_OUTPUT_BYTES: String(maximum),
    REPOSITORIES: info.repositories.map(repo => `- ${repo.path} (prepared HEAD: ${repo.revision})`).join('\n')};
  return read('../prompts/code-agent-system.md').replace(/\{\{([A-Z_]+)\}\}/g, (_, key) => {
    if (!(key in values)) throw new Error(`Missing prompt variable ${key}`);
    return values[key];
  });
}

export async function apply(ctx, config) {
  const settingsPath = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'ascend', 'workspace.json');
  const saved = existsSync(settingsPath) ? JSON.parse(readFileSync(settingsPath, 'utf8')) : {};
  config = {...config, container: config.container || process.env.DSH_ASCEND_CONTAINER || saved.container
    || 'dsh-ascend-workspace'};
  if (!config.workdir.startsWith('/') || !Number.isInteger(config.maxOutputBytes)
      || config.maxOutputBytes < 1 || config.maxOutputBytes > 150000) throw new Error('Invalid Ascend configuration');
  const lifetime = new AbortController();
  const operations = new Set(), watches = new Map();
  ctx.effect(() => async () => {
    lifetime.abort(new Error('Ascend preset unloaded'));
    await Promise.allSettled([...operations]);
  });
  const docker = new Docker(config, scrubbedParentEnv());
  const info = await docker.connect(lifetime.signal);
  ctx.systemPrompt.section({name: PERSONA_PREFIX_SECTION,
    order: ctx.systemPrompt.getSectionOrder('DEPLOYMENT_PERSONA_PREFIX'),
    text: renderPrompt(info, config.maxOutputBytes), complete: true, interpolate: false});
  ctx.systemPrompt.suppressRuntimeContext();
  ctx.on('system-prompt/assemble', async (_assembly, _context, next) => {
    const assembly = await next();
    assembly.contexts = assembly.contexts.filter(item => item.name !== 'working-directory:current');
    return assembly;
  }, {prepend: true});
  ctx.tools.restrict({allow: []});
  await mcp.apply(ctx, mcp.Config({serverName: 'ascend_github', transport: 'stdio',
    command: config.githubCommand || process.env.GITHUB_MCP_COMMAND || saved.githubCommand || 'github-mcp',
    args: ['--backend', config.githubBackend || process.env.GITHUB_MCP_BACKEND || 'rest'],
    env: Object.fromEntries(['GH_TOKEN', 'GITHUB_TOKEN', 'GITHUB_PERSONAL_ACCESS_TOKEN']
      .map(key => [key, process.env[key] || ''])),
    cwd: process.cwd(), toolCallTimeoutMs: 180000, failOnStartupError: true}));

  const owned = promise => {
    operations.add(promise);
    promise.then(() => operations.delete(promise), () => operations.delete(promise));
    return promise;
  };
  const request = (session, args, signal = lifetime.signal) => docker.request({
    session, workdir: config.workdir, max_output_bytes: config.maxOutputBytes, ...args,
  }, signal);

  function watch(agent) {
    if (!agent || watches.has(agent.id)) return;
    const control = new AbortController();
    const signal = AbortSignal.any([lifetime.signal, control.signal]);
    watches.set(agent.id, control);
    owned((async () => {
      while (!signal.aborted) {
        try {
          const tasks = await request(agent.id, {action: 'list'}, signal);
          for (const state of tasks) {
            if (!terminal(state)) continue;
            const message = createUserMessage({source: {kind: 'dsh-ascend'}, content: [{type: 'text',
              text: `<task-notification>\nTask ${state.task_id}: ${state.status}; exit code ${state.exit_code}.\n`
                + `Command: ${state.description}\nOutput: ${state.output_file}\n${state.content}\n</task-notification>`}]});
            agent.steer(message);
            await request(agent.id, {action: 'ack', task_id: state.task_id}, signal);
          }
        } catch (error) {
          if (!signal.aborted) ctx.logger.warn(`Ascend task notification: ${error.message}`);
        }
        await delay(1000, undefined, {signal}).catch(() => {});
      }
    })()).finally(() => watches.delete(agent.id));
  }
  ctx.on('agent/created', ({agent}) => watch(agent));
  ctx.on('agent/disposed', ({agent}) => watches.get(agent.id)?.abort());

  ctx.tools.register({
    name: 'bash', description: bashDescription, parameters: bashSchema,
    isConcurrencySafe: () => true,
    timeoutMs: 660000,
    output: {schema: {}, render: (_args, value) => [{type: 'text', text: value.content}]},
    async execute(args, exec) {
      args = normalizeBash(args);
      return owned((async () => {
        const session = exec.agent?.id ?? 'operator';
        const task_id = randomUUID().replaceAll('-', '');
        const signal = AbortSignal.any([exec.signal, lifetime.signal]);
        let state;
        try {
          state = await request(session, {action: 'start', task_id, arguments: args}, signal);
          while (!terminal(state) && !state.background) {
            await delay(200, undefined, {signal});
            state = await request(session, {action: 'poll', task_id}, signal);
          }
          if (state.background && (args.run_in_background === true || args.run_in_background === 'true'
              || !terminal(state))) {
            await request(session, {action: 'track', task_id}, signal);
            watch(exec.agent);
          }
        } catch (error) {
          if (signal.aborted) {
            try {
              const cleanup = AbortSignal.timeout(15000);
              state = await request(session, {action: 'poll', task_id}, cleanup);
              if (!terminal(state) && !state.background) {
                await request(session, {action: 'stop', task_id}, cleanup);
                do {
                  await delay(100, undefined, {signal: cleanup});
                  state = await request(session, {action: 'poll', task_id}, cleanup);
                } while (!terminal(state));
              } else if (state.background) {
                await request(session, {action: 'track', task_id}, cleanup);
                watch(exec.agent);
              }
            } catch (cleanupError) { ctx.logger.warn(`Ascend cancellation cleanup: ${cleanupError.message}`); }
          }
          throw error;
        }
        if (state.failed) throw new Error(`${state.content}\nExit code: ${state.exit_code}; log: ${state.output_file}`);
        return state;
      })());
    },
  });
}
