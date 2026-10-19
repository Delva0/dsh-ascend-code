/** Prepare the workspace and install its bundle into one dsh profile. */
import {spawnSync} from 'node:child_process';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {homedir} from 'node:os';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {join, resolve} from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf(name);
  if (index < 0) return fallback;
  if (!args[index + 1] || args[index + 1].startsWith('--')) throw new Error(`${name} requires a value`);
  return args[index + 1];
};
const profile = flag('--profile', 'web');
const container = flag('--container', 'dsh-ascend-workspace');
const volume = flag('--volume', 'dsh-ascend-workspace');
const image = flag('--image', 'dsh-ascend:0.1.0');
if (args.includes('--help')) {
  console.log('node scripts/setup.mjs [--profile web] [--container NAME] [--volume NAME] [--image TAG] [--skip-plugin]');
  process.exit(0);
}
function run(argv, {capture = false, allowFailure = false, env = process.env} = {}) {
  const result = spawnSync(argv[0], argv.slice(1), {cwd: root, env, encoding: 'utf8',
    stdio: capture ? ['ignore', 'pipe', 'pipe'] : 'inherit'});
  if (!allowFailure && (result.error || result.status !== 0)) {
    throw new Error(result.error?.message || result.stderr || `${argv[0]} exited ${result.status}`);
  }
  return capture ? (result.status === 0 ? result.stdout.trim() : null) : result.status;
}
if (Number(process.versions.node.split('.')[0]) < 24) throw new Error('Node.js 24+ is required');
for (const command of [['docker', 'info'], ['gh', 'auth', 'status'], ['uv', '--version'], ['pnpm', '--version']]) {
  run(command, {capture: true});
}
const dshVersion = run(['dsh', '--version'], {capture: true, allowFailure: true});
if (!dshVersion) run(['npm', 'install', '--global', '@deepseek-ai/dsh@0.2.1-rc.2']);
else if (!dshVersion.includes('0.2.1-rc.2')) throw new Error(`Expected dsh 0.2.1-rc.2, found ${dshVersion}`);

mkdirSync(join(root, 'build/wheels'), {recursive: true});
const wheel = join(root, 'build/wheels/github_mcp-0.1.1-py3-none-any.whl');
run(['gh', 'release', 'download', 'v0.1.1', '--repo', 'Delva0/github-mcp', '--pattern',
  'github_mcp-0.1.1-py3-none-any.whl', '--output', wheel, '--clobber']);
if (createHash('sha256').update(readFileSync(wheel)).digest('hex') !==
    '192a06ccfe0c98ebd6e543a455af0f0e437b83956224bcf95c86f2be1f247ec1') throw new Error('GitHub MCP checksum mismatch');
// A private runtime directory keeps this pinned dependency independent of the user's uv tools.
const githubEnv = resolve(root, 'build/github-mcp');
run(['uv', 'venv', '--python', '3.13', '--allow-existing', githubEnv]);
run(['uv', 'pip', 'install', '--python', join(githubEnv, 'bin/python'), wheel]);
run(['docker', 'build', '-t', image, '.']);
const existing = run(['docker', 'inspect', '--type', 'container', container], {capture: true, allowFailure: true});
if (existing) {
  const data = JSON.parse(existing)[0];
  if (data.Config.Labels?.['io.delva.dsh-ascend'] !== '0.1.0'
      || !data.Mounts.some(m => m.Type === 'volume' && m.Name === volume && m.Destination === '/workspace')) {
    throw new Error(`Existing container ${container} does not match this workspace; choose a different name/volume`);
  }
  if (!data.State.Running) run(['docker', 'start', data.Id]);
} else {
  run(['docker', 'volume', 'create', volume]);
  run(['docker', 'run', '-d', '--name', container, '--restart', 'unless-stopped',
    '--mount', `type=volume,src=${volume},dst=/workspace`, image]);
}
run(['docker', 'exec', container, '/opt/ascend/.venv/bin/python', '/opt/ascend/prepare_workspace.py']);
run(['docker', 'exec', container, '/opt/ascend/.venv/bin/lsp', '--version']);
const settingsDir = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'ascend');
mkdirSync(settingsDir, {recursive: true});
writeFileSync(join(settingsDir, 'workspace.json'), JSON.stringify({container,
  githubCommand: join(githubEnv, 'bin/github-mcp')}, null, 2) + '\n');
if (!args.includes('--skip-plugin')) {
  run(['npm', 'pack', '--pack-destination', 'build']);
  run(['dsh', 'plugin', '--profile', profile, 'add', resolve(root, 'build/delva-dsh-ascend-0.1.0.tgz')]);
}
console.log('\nWorkspace ready. Launch with:');
console.log(`dsh ${profile}`);
console.log('Select vLLM Ascend in a new session before the first message.');
