/** Native registration and image transport for the unchanged upstream web tools. */
import {readFileSync} from 'node:fs';
import {readFile} from 'node:fs/promises';
import {basename} from 'node:path';
import {createWorkerPool} from './worker.mjs';

export function webSearchEnvironment(config = {}, environment = process.env) {
  const backend = config.webSearchBackend ?? (environment.DSH_ASCEND_WEB_SEARCH_BACKEND?.trim() || 'brave');
  const concurrency = Number(config.webSearchConcurrency
    ?? (environment.DSH_ASCEND_WEB_SEARCH_CONCURRENCY?.trim() || '1'));
  const interval = Number(config.webSearchInterval
    ?? (environment.DSH_ASCEND_WEB_SEARCH_INTERVAL?.trim() || '2'));
  if (!['auto', 'brave', 'duckduckgo'].includes(backend)) throw new Error('Invalid web search backend');
  if (!Number.isSafeInteger(concurrency) || concurrency < 1) throw new Error('Invalid web search concurrency');
  if (!Number.isFinite(interval) || interval < 0) throw new Error('Invalid web search interval');
  return {...Object.fromEntries(['BRAVE_SEARCH_API_KEY', 'BRAVE_API_KEY']
    .filter(key => environment[key]).map(key => [key, environment[key]])),
    DSH_ASCEND_WEB_SEARCH_BACKEND: backend,
    DSH_ASCEND_WEB_SEARCH_CONCURRENCY: String(concurrency),
    DSH_ASCEND_WEB_SEARCH_INTERVAL: String(interval)};
}

export function registerWebSearch(ctx, {ready, signal, config = {}}) {
  const spec = JSON.parse(readFileSync(new URL('../assets/web-search.json', import.meta.url), 'utf8'));
  const run = createWorkerPool(ctx, {backend: 'web_search', ready, signal,
    module: 'ascend_github.web_worker', label: 'web_search', env: webSearchEnvironment(config)});
  ctx.tools.register({
    name: spec.name, description: spec.description, parameters: spec.parameters,
    isConcurrencySafe: () => true, timeoutMs: 180000,
    output: {schema: spec.returns, render: (_args, value) => [{type: 'text', text: JSON.stringify(value)}]},
    presentCall(args) {
      const queries = Array.isArray(args?.queries) ? args.queries : [];
      const label = queries.length === 1 ? queries[0]?.query : undefined;
      return {card: 'generic', kind: 'search', title: label ? `web_search ${label}` : 'web_search',
        rawInput: queries};
    },
    async execute(args, exec) {
      return (await run(args, exec)).value;
    },
  });
}

export function registerWebFetch(ctx, {ready, signal}) {
  const spec = JSON.parse(readFileSync(new URL('../assets/web-fetch.json', import.meta.url), 'utf8'));
  const run = createWorkerPool(ctx, {backend: 'web_fetch', ready, signal,
    module: 'ascend_github.web_worker', label: 'web_fetch'});
  const projections = new WeakMap();
  ctx.tools.register({
    name: spec.name, description: spec.description, parameters: spec.parameters,
    isConcurrencySafe: () => true, timeoutMs: 180000,
    output: {schema: spec.returns, render: (_args, value) => [{type: 'text', text: JSON.stringify(value)}]},
    presentCall(args) {
      const requests = Array.isArray(args?.requests) ? args.requests : [];
      const label = requests.length === 1 ? requests[0]?.url || requests[0]?.ref : undefined;
      return {card: 'generic', kind: 'fetch', title: label ? `web_fetch ${label}` : 'web_fetch',
        rawInput: requests};
    },
    async execute(args, exec) {
      const operation = AbortSignal.any([exec.signal, signal]);
      // Use the route that produced this tool call, including UI model switches.
      const route = exec.agent?.session?.requestHeader()?.config ?? exec.agent?.options;
      const model = route?.provider && route?.model
        ? await ctx.llm.resolveModelInfo(route.provider, route.model, operation) : undefined;
      const reply = await run(args, exec, {image_input: model?.inputModalities?.includes('image') === true});
      const content = [];
      for (const part of reply.image_parts ?? []) {
        operation.throwIfAborted();
        if (part.type === 'input_text') content.push({type: 'text', text: part.text});
        else if (part.type === 'input_image') {
          const data = await readFile(part.image_url, {signal: operation});
          const attachment = await ctx.attachments.saveImage({data, mediaType: part.media_type,
            name: basename(part.image_url)});
          operation.throwIfAborted();
          content.push({type: 'image', attachment});
        } else throw new Error('Invalid web_fetch image projection');
      }
      if (content.length) projections.set(exec, content);
      return reply.value;
    },
    projectContent(exec, result) {
      const content = projections.get(exec);
      projections.delete(exec);
      if (!result.isError && content) return [...result.content, ...content];
    },
  });
}
