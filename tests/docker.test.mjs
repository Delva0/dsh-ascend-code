import {test} from 'node:test';
import assert from 'node:assert/strict';
import {command} from '../plugin/docker.mjs';

test('argv is passed literally without shell interpolation', async () => {
  const literal = 'a; $(touch /tmp/should-not-exist) `id` $HOME';
  assert.equal(await command([process.execPath, '-e', 'process.stdout.write(process.argv[1])', literal]), literal);
});
test('command failures preserve diagnostics', async () => {
  await assert.rejects(command([process.execPath, '-e', 'process.stderr.write("expected"); process.exit(4)']), /expected/);
});
test('abort terminates and reaps the transport', async () => {
  const control = new AbortController();
  const pending = command([process.execPath, '-e', 'setInterval(()=>{}, 1000)'], {signal: control.signal});
  control.abort(new Error('cancel test'));
  await assert.rejects(pending, /cancel test/);
});
test('a missing binary rejects without hanging', async () => {
  await assert.rejects(command(['/definitely-missing-ascend-command']), /ENOENT/);
});
