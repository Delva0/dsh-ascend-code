import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {bashDescription, bashSchema, normalizeBash, renderPrompt} from '../plugin/index.mjs';

test('prompt substitution preserves the complete upstream text and Bash schema', () => {
  const template = readFileSync('prompts/code-agent-system.md', 'utf8');
  const info = {workdir: '/workspace', repositories: [{path: '/workspace/vllm', revision: 'abc'}]};
  assert.equal(renderPrompt(info, 100000), template.replaceAll('{{WORKDIR}}', '/workspace')
    .replaceAll('{{REPOSITORIES}}', '- /workspace/vllm')
    .replaceAll('{{BASH_MAX_OUTPUT_BYTES}}', '100000'));
  assert.equal(bashDescription, readFileSync('prompts/bash.md', 'utf8'));
  assert.deepEqual(bashSchema, JSON.parse(readFileSync('assets/bash-schema.json', 'utf8')));
});
test('v5 numeric and boolean normalization precedes strict validation', () => {
  assert.deepEqual(normalizeBash({command:'pwd',timeout:'12.5',run_in_background:'false'}),
    {command:'pwd',timeout:12.5,run_in_background:false});
  for (const args of [null, [], {}, {command:'pwd',workdir:'/'}, {command:'pwd',timeout:null},
    {command:'pwd',description:null}, {command:'pwd',timeout:'NaN'}, {command:'pwd',run_in_background:1}]) {
    assert.throws(() => normalizeBash(args));
  }
});
