"""Offline tests of deploy_amp.py against a fake AMP (no network). By BONE."""
import importlib.util, io, os, sys, zipfile, tarfile, time

spec = importlib.util.spec_from_file_location('d', sys.argv[1])
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
CSS_ZIP, MM_TAR = sys.argv[2], sys.argv[3]
_clock = [time.time()]


def _sleep(s):
    _clock[0] += s


d.time.sleep = _sleep  # a fake clock: sleeping only moves it forward
d.time.time = lambda: _clock[0]
d.T0 = _clock[0]
os.environ.update(AMP_URL='https://x', AMP_USER='u', AMP_PASSWORD='p', AMP_TARGETS='a:1:canary,b:2:main', AMP_GAME_HOST='h')


class FakeAmp:
    """Folders as sets of names; files as {path: size}. Behavior knobs per instance."""
    worlds = {}

    def __init__(self, iid):
        self.w = FakeAmp.worlds[iid]
        self.w.setdefault('calls', [])

    def call(self, method, body=None, tries=3):
        self.w['calls'].append(method)
        if method in self.w.get('fail_on', ()):
            raise RuntimeError(f'AMP refused {method}')
        if method == 'Core/Stop':
            self.w['state'] = d.STOPPED
        if method == 'Core/Start':
            self.w['state'] = self.w.get('start_state', d.RUNNING)
            self.w['console'] = list(self.w.get('boot_console', []))
        if method == 'Core/GetUpdates':
            out, self.w['console'] = self.w.get('console', []), []
            return {'ConsoleEntries': [{'Contents': c} for c in out]}
        if method == 'FileManagerPlugin/ExtractArchive':
            for path, size in self.w['payload_files'].items():
                folder, name = path.rsplit('/', 1)
                self.w['fs'].setdefault(folder, {})[name] = size
                parent, leaf = folder.rsplit('/', 1)
                while parent.startswith('game/csgo/addons'):
                    self.w['fs'].setdefault(parent, {})[leaf] = 0
                    if '/' not in parent:
                        break
                    parent, leaf = parent.rsplit('/', 1)
        if method == 'FileManagerPlugin/TrashDirectory':
            p = body['DirectoryName']
            folder, name = p.rsplit('/', 1)
            self.w['fs'].get(folder, {}).pop(name, None)
            for k in [k for k in self.w['fs'] if k == p or k.startswith(p + '/')]:
                del self.w['fs'][k]
        return {}

    def state(self):
        return self.w['state']

    def ls(self, folder):
        return {k: {'Filename': k, 'SizeBytes': v} for k, v in self.w['fs'].get(folder, {}).items()}

    def ok(self, method, body):
        self.call(method, body)
        return True

    def write(self, path, data, chunk=0):
        self.w['calls'].append('write')
        if 'write' in self.w.get('fail_on', ()):
            raise RuntimeError('upload failed')
        _, self.w['payload_files'] = d.payload_for(CSS_ZIP if self.w['has_css'] else None, MM_TAR)

    def rename_dir(self, path, new):
        folder, name = path.rsplit('/', 1)
        fs = self.w['fs']
        if name not in fs.get(folder, {}):
            return False
        fs[folder][new] = fs[folder].pop(name)
        for k in [k for k in fs if k == path or k.startswith(path + '/')]:
            fs[folder + '/' + new + k[len(path):]] = fs.pop(k)
        return True

    def console(self):
        return [e['Contents'] for e in self.call('Core/GetUpdates')['ConsoleEntries']]

    def stop(self):
        self.call('Core/Stop')
        return True


def world(**kw):
    fs = {'game/csgo/addons': {'counterstrikesharp': 0, 'metamod': 0},
          d.CSS: {'api': 0, 'bin': 0, 'gamedata': 0, 'configs': 0, 'plugins': 0},
          d.MM: {'bin': 0, 'counterstrikesharp.vdf': 1}}
    w = dict(fs=fs, state=d.RUNNING, has_css=True, boot_console=['CounterStrikeSharp is starting up'])
    w.update(kw)
    return w


d.Amp = FakeAmp
players = {}
d.humans = lambda host, port: players.get(port, 0)


def run(targets='a:1:canary,b:2:main'):
    os.environ['AMP_TARGETS'] = targets
    sys.argv = ['deploy_amp.py', CSS_ZIP, MM_TAR]
    try:
        d.main()
    except SystemExit as e:
        return e.code


def folders(w):
    return sorted(w['fs'][d.CSS]), sorted(w['fs'][d.MM])


# 1. happy path, then a rerun sees "already on this build"
FakeAmp.worlds = {'a': world(), 'b': world()}
assert run() == 0, 'happy path'
print('happy:', folders(FakeAmp.worlds['b']))
assert run() == 0 and 'Core/Stop' not in FakeAmp.worlds['a']['calls'][-5:], 'rerun should not stop anything'

# 2. crash on the canary: rolled back, started, main untouched
FakeAmp.worlds = {'a': world(boot_console=['Segmentation fault (core dumped)']), 'b': world()}
assert run() == 2, 'crash'
assert folders(FakeAmp.worlds['a'])[0] == ['api', 'bin', 'configs', 'gamedata', 'plugins'], folders(FakeAmp.worlds['a'])
assert FakeAmp.worlds['a']['calls'][-1] == 'Core/Start' and FakeAmp.worlds['b'].get('calls', []) == [], 'main must be untouched'
print('crash: rolled back', folders(FakeAmp.worlds['a']))

# 3. AMP error mid-swap (upload refused): rolled back, started, exit 3
FakeAmp.worlds = {'a': world(fail_on=('write',)), 'b': world()}
assert run() == 3, 'interrupted'
assert folders(FakeAmp.worlds['a'])[0] == ['api', 'bin', 'configs', 'gamedata', 'plugins'] and 'bin' in FakeAmp.worlds['a']['fs'][d.MM]
assert FakeAmp.worlds['a']['calls'][-1] == 'Core/Start'
print('interrupted: rolled back and started')

# 4. canary busy: skipped, main untouched, exit 3
FakeAmp.worlds = {'a': world(), 'b': world()}
players[1] = 5
os.environ['AMP_WAIT_EMPTY_MINUTES'] = '0'
assert run() == 3 and FakeAmp.worlds['b'].get('calls', []) == [], 'canary skipped must stop the rollout'
players.clear()
print('canary busy: rollout stopped')

# 5. CSS switched off (no .vdf): deploys without expecting CSS to start
w = world(boot_console=[])
del w['fs'][d.MM]['counterstrikesharp.vdf']
FakeAmp.worlds = {'a': w}
assert run('a:1:canary') == 0, 'css off'
print('css off: deployed')

# 6. server without CSS: only Metamod moves
w = world()
w['has_css'] = False
del w['fs']['game/csgo/addons']['counterstrikesharp']
del w['fs'][d.CSS]
w['boot_console'] = []
FakeAmp.worlds = {'a': w}
assert run('a:1:canary') == 0
assert not any(k.startswith(d.CSS) for k in w['fs']), 'must not create a CSS folder'
print('no css: metamod only', sorted(w['fs'][d.MM]))
print('ALL OK')
