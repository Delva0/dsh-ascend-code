/** Verify the installed bundle through dsh's real browser API, without a model. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {setTimeout as delay} from 'node:timers/promises';

const child = spawn('dsh', ['--profile', process.env.DSH_TEST_PROFILE || 'web',
  '--no-open', '--host', '127.0.0.1', '--port', '0'], {stdio: ['ignore', 'pipe', 'pipe']});
let log = '', failure;
child.on('error', error => { failure = error; });
for (const stream of [child.stdout, child.stderr]) stream.on('data', part => { log += part; });
const exited = new Promise(resolve => child.once('close', resolve));
const deadline = Date.now() + 45000;
const redactedLog = () => log.replace(/(token=)[^\s&]+/g, '$1[redacted]');
try {
  let launch;
  while (!(launch = log.match(/dsh web: (http:\/\/127\.0\.0\.1:\d+\/\?token=\S+)/)?.[1])) {
    if (failure) throw failure;
    assert.equal(child.exitCode, null, redactedLog());
    assert.equal(child.signalCode, null, redactedLog());
    assert(Date.now() < deadline, `dsh startup timed out\n${redactedLog()}`);
    await delay(100);
  }
  const auth = await fetch(launch, {redirect: 'manual', signal: AbortSignal.timeout(15000)});
  assert.equal(auth.status, 303);
  const cookie = auth.headers.get('set-cookie').split(';')[0];
  const base = new URL(launch).origin;
  const page = await fetch(base, {headers: {cookie}, signal: AbortSignal.timeout(15000)});
  assert.equal(page.status, 200);
  assert((await page.text()).includes('__DSH_BOOT__'));
  const method = 'agentPresets/list';
  const response = await fetch(`${base}/api/${method}`, {method: 'POST',
    headers: {cookie, 'content-type': 'application/json', origin: base},
    body: JSON.stringify({type: 'client-request', rpcId: 'ascend-web-smoke', method, payload: {args: {}}}),
    signal: AbortSignal.timeout(15000)});
  assert.equal(response.status, 200);
  const {result} = await response.json();
  assert(result.ok, JSON.stringify(result));
  const preset = result.value.presets.find(item => item.id === 'ascend');
  assert(preset, 'Installed bundle did not contribute the Ascend preset');
  assert.equal(preset.broken, undefined, JSON.stringify(preset));
  assert.equal(preset.name, 'vLLM Ascend');
  console.log('PASS: installed dsh Web bundle, authenticated UI/API, healthy vLLM Ascend preset');
} finally {
  child.kill('SIGTERM');
  const force = setTimeout(() => child.kill('SIGKILL'), 5000);
  await exited;
  clearTimeout(force);
}
