/** Native registration and image transport for the unchanged upstream web_fetch. */
import {readFileSync} from 'node:fs';
import {readFile} from 'node:fs/promises';
import {basename} from 'node:path';
import {createWorkerPool} from './worker.mjs';

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
