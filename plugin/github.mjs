/** Native dsh GitHub tool backed by the unchanged Python implementation. */
import {readFileSync} from 'node:fs';
import {createWorkerPool} from './worker.mjs';
export {PythonWorker as GitHubWorker} from './worker.mjs';

const text = value => [{type: 'text', text: JSON.stringify(value)}];

export function registerGitHub(ctx, {backend, ready, signal}) {
  const spec = JSON.parse(readFileSync(new URL(`../assets/github-${backend}.json`, import.meta.url), 'utf8'));
  // dsh's schema subset uses oneOf for nullable values. Error-only DSL results
  // throw before output validation; every successful DSL value has data.
  const outputSchema = backend === 'dsl' ? {type: 'object', required: ['data'], properties: {
    ...spec.returns.properties, data: {oneOf: [{type: 'object'}, {type: 'null'}]},
  }} : spec.returns;
  const env = Object.fromEntries(['GH_TOKEN', 'GITHUB_TOKEN', 'GITHUB_PERSONAL_ACCESS_TOKEN']
    .filter(key => process.env[key]).map(key => [key, process.env[key]]));
  const run = createWorkerPool(ctx, {backend, ready, signal, env});
  ctx.tools.register({
    name: 'github', description: spec.description, parameters: spec.parameters,
    isConcurrencySafe: () => true, timeoutMs: 180000,
    output: {schema: outputSchema, render: (_args, value) => text(value)},
    presentCall(args) {
      const requests = Array.isArray(args?.requests) ? args.requests : [];
      const label = requests.length === 1 ? requests[0]?.path : undefined;
      return {card: 'generic', kind: 'fetch', title: label ? `GitHub ${label}` : 'GitHub',
        rawInput: requests.length ? requests : args?.query};
    },
    async execute(args, exec) {
      // The original registry validates batch members individually.
      return (await run(args, exec)).value;
    },
  });
}
