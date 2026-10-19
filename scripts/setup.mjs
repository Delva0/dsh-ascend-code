/** Optional prewarming and local-checkout installation; the plugin also prepares itself. */
import {spawnSync} from 'node:child_process';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {homedir} from 'node:os';
import {fileURLToPath} from 'node:url';
import {join, resolve} from 'node:path';
import {ensureWorkspace} from '../plugin/provision.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const {version} = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf(name);
  if (index < 0) return fallback;
  if (!args[index + 1] || args[index + 1].startsWith('--')) throw new Error(`${name} requires a value`);
  return args[index + 1];
};
const profile = flag('--profile', 'web');
const container = flag('--container', 'dsh-ascend-workspace');
if (args.includes('--help')) {
  console.log('node scripts/setup.mjs [--profile web] [--container NAME] [--volume NAME] [--image TAG] [--skip-plugin]');
  process.exit(0);
}
function run(argv, capture = false) {
  const result = spawnSync(argv[0], argv.slice(1), {cwd: root, encoding: 'utf8',
    stdio: capture ? ['ignore', 'pipe', 'pipe'] : 'inherit'});
  if (result.error || result.status !== 0) throw new Error(result.error?.message || result.stderr
    || `${argv[0]} exited ${result.status}`);
  return result.stdout?.trim();
}
if (Number(process.versions.node.split('.')[0]) < 24) throw new Error('Node.js 24+ is required');
if (!run(['dsh', '--version'], true).includes('0.2.1-rc.2')) throw new Error('dsh 0.2.1-rc.2 is required');
const settings = await ensureWorkspace({container, volume: flag('--volume', container),
  image: flag('--image', 'dsh-ascend:0.1.0')});
const settingsDir = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'ascend');
mkdirSync(settingsDir, {recursive: true});
const {container: savedContainer, volume, image} = settings;
writeFileSync(join(settingsDir, 'workspace.json'), JSON.stringify({container: savedContainer, volume, image}, null, 2) + '\n');
if (!args.includes('--skip-plugin')) {
  mkdirSync(join(root, 'build'), {recursive: true});
  run(['npm', 'pack', '--pack-destination', 'build']);
  const content = readFileSync(resolve(root, `build/delva-dsh-ascend-${version}.tgz`));
  const digest = createHash('sha256').update(content).digest('hex').slice(0, 12);
  // pnpm caches a local tarball by its path; each development build needs its own path.
  const archive = resolve(root, `build/delva-dsh-ascend-${version}-${digest}.tgz`);
  writeFileSync(archive, content);
  run(['dsh', 'plugin', '--profile', profile, 'add', archive]);
}
console.log(`\nWorkspace ready. Run dsh ${profile} and select vLLM Ascend in a new session.`);
