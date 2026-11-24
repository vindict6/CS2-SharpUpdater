#!/usr/bin/env python3
"""Deploy a SharpUpdater build to CS2 servers run by CubeCoders AMP, one at a time, and roll back on a crash. By BONE.

    deploy_amp.py <counterstrikesharp zip> [metamod tar.gz]

Environment:
  AMP_URL, AMP_USER, AMP_PASSWORD   an AMP user with FileManager and Start/Stop on the instances
  AMP_TOTP_SECRET                   that user's 2FA secret (base32), if it has 2FA
  AMP_TARGETS                       instance-id:game-port[:name], comma separated, in order. The first is the canary: if
                                    it can't be deployed (busy, crashed) nothing after it is touched
  AMP_GAME_HOST                     the servers' public IP, for the "anyone playing?" check
  AMP_WAIT_EMPTY_MINUTES            how long to wait for a server to empty (default 120); still busy = skipped
  AMP_WATCH_MINUTES                 how long to watch a server once it's running (default 4)
  AMP_DEADLINE_MINUTES              stop starting new servers after this long (default 300; GitHub kills jobs at 360)
  DEPLOY_TAG                        names the backups (default: the zip's version)

What it changes, per server: CounterStrikeSharp's engine parts (addons/counterstrikesharp/api, bin, gamedata) where
CounterStrikeSharp is installed, and Metamod's binaries (addons/metamod/bin). Configs, plugins and Metamod's
.vdf/metaplugins.ini are never touched; the old folders are kept as <name>.<tag>bak. Once a server is stopped, any
failure (a crash, an AMP error, the job being canceled) puts the old folders back and starts it again.

Exit codes: 0 every server deployed (or already on this build); 2 a server crashed on the build and was rolled back;
3 a server was skipped (busy, out of time) or the rollout stopped on an error, so a later run should try again;
4 a rollback could not be completed: a server needs a person.
"""
import base64, hashlib, io, json, os, posixpath, re, signal, socket, ssl, struct, sys, tarfile, time, urllib.request, zipfile

CSS = 'game/csgo/addons/counterstrikesharp'
MM = 'game/csgo/addons/metamod'
CSS_PARTS = ('api', 'bin', 'gamedata')
STOPPED, RUNNING, FAILED = 0, 20, 100
T0 = time.time()


class Crashed(Exception):
    """The server crashed on the new build."""


class RollbackFailed(Exception):
    """The old build could not be put back."""


def say(*x):
    print(time.strftime('%H:%M:%S'), *x, flush=True)


def _sigterm(signum, frame):
    raise SystemExit('canceled (SIGTERM)')


def totp(secret):
    key = base64.b32decode(secret.replace(' ', '').upper() + '=' * (-len(secret.replace(' ', '')) % 8))
    digest = __import__('hmac').new(key, struct.pack('>Q', int(time.time() // 30)), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f'{(struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}'


class Amp:
    def __init__(self, instance):
        self.prefix = f"{os.environ['AMP_URL'].rstrip('/')}/API/ADSModule/Servers/{instance}/API"
        self.sid = None
        self.login()

    def login(self):
        secret = os.environ.get('AMP_TOTP_SECRET', '')
        r = None
        for attempt in range(2):
            self.sid = None
            r = self.post('Core/Login', {'username': os.environ['AMP_USER'], 'password': os.environ['AMP_PASSWORD'],
                                         'token': totp(secret) if secret else '', 'rememberMe': False})
            if (r or {}).get('success') or not secret or (r or {}).get('result') != 40 or attempt:
                break
            time.sleep(31 - time.time() % 30)  # that code was just used: wait for the next one
        if not (r or {}).get('success'):
            raise RuntimeError(f'AMP login failed (result {(r or {}).get("result")})')
        self.sid = r['sessionID']

    def post(self, method, body):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if self.sid:
            headers['Authorization'] = f'Bearer {self.sid}'
        req = urllib.request.Request(f'{self.prefix}/{method}', data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as r:
            text = r.read().decode('utf-8', 'replace')
        return json.loads(text) if text.strip() else None

    def call(self, method, body=None, tries=3):
        """Retries network errors; an AMP refusal raises at once."""
        for attempt in range(tries):
            try:
                r = self.post(method, body or {})
            except (OSError, ValueError) as e:
                if attempt == tries - 1:
                    raise RuntimeError(f'AMP {method}: {e}') from e
                time.sleep(3 * (attempt + 1))
                continue
            if isinstance(r, dict) and r.get('Title') == 'Unauthorized Access':
                raise RuntimeError(f'AMP refused {method}: {r.get("Message", "")[:200]}')
            return r

    def state(self):
        try:
            return (self.call('Core/GetStatus') or {}).get('State')
        except RuntimeError:
            return None

    def ls(self, d):
        r = self.call('FileManagerPlugin/GetDirectoryListing', {'Dir': d})
        r = r.get('result', r) if isinstance(r, dict) else r
        return {x['Filename']: x for x in r} if isinstance(r, list) else {}

    def ok(self, method, body):
        r = self.call(method, body)
        r = r.get('result', r) if isinstance(r, dict) and 'Status' not in r else r
        return bool(r is True or (isinstance(r, dict) and r.get('Status')))

    def write(self, path, data, chunk=1_000_000):
        for off in range(0, len(data), chunk):
            if not self.ok('FileManagerPlugin/WriteFileChunk', {'Filename': path, 'Offset': off,
                                                                'Data': base64.b64encode(data[off:off + chunk]).decode(),
                                                                'FinalChunk': off + chunk >= len(data)}):
                raise RuntimeError(f'upload of {path} failed at byte {off}')

    def rename_dir(self, path, new_name):
        return self.ok('FileManagerPlugin/RenameDirectory', {'oldDirectory': path, 'NewDirectoryName': new_name})

    def console(self):
        return [e.get('Contents') or '' for e in (self.call('Core/GetUpdates') or {}).get('ConsoleEntries') or []]

    def stop(self):
        """Stop and wait for Stopped, asking again if AMP is busy restarting a crashed server."""
        for _ in range(3):
            self.call('Core/Stop')
            for _ in range(60):
                if self.state() == STOPPED:
                    return True
                time.sleep(2)
        return False


def humans(host, port):
    """Human players from the Steam query, None if the server doesn't answer."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        q = b'\xff\xff\xff\xffTSource Engine Query\x00'
        s.sendto(q, (host, port))
        d, _ = s.recvfrom(4096)
        if d[4] == 0x41:
            s.sendto(q + d[5:9], (host, port))
            d, _ = s.recvfrom(4096)
        p = 6
        for _ in range(4):
            p = d.index(b'\x00', p) + 1
        return d[p + 2] - d[p + 4]
    except (OSError, ValueError, IndexError):
        return None


def payload_for(css_zip, mm_tar):
    """A zip laid out from game/csgo with only the given parts, and {path: size} of every file in it."""
    buf, files = io.BytesIO(), {}
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as out:
        if css_zip:
            z = zipfile.ZipFile(css_zip)
            for n in z.namelist():
                parts = n.split('/')
                if len(parts) > 3 and parts[:2] == ['addons', 'counterstrikesharp'] and parts[2] in CSS_PARTS and not n.endswith('/'):
                    data = z.read(n)
                    out.writestr(n, data)
                    files[f'game/csgo/{n}'] = len(data)
        if mm_tar:
            with tarfile.open(mm_tar) as t:
                for m in t.getmembers():
                    name = m.name[2:] if m.name.startswith('./') else m.name
                    if m.isfile() and name.startswith('addons/metamod/bin/'):
                        data = t.extractfile(m).read()
                        out.writestr(name, data)
                        files[f'game/csgo/{name}'] = len(data)
    return buf.getvalue(), files


def wait_until_empty(a, host, port, name, minutes):
    """True once the Steam query says nobody is playing, or the server is plainly down (AMP doesn't report it running).
    A server that is running but doesn't answer may be changing maps with players loading in, so it isn't counted
    as empty until it has stayed silent for 5 minutes."""
    deadline, silent_since = time.time() + minutes * 60, None
    while True:
        h = humans(host, port)
        if h == 0:
            return True
        if h is None:
            if a.state() != RUNNING:
                say(f'[{name}] not running (AMP state {a.state()}); nobody can be playing')
                return True
            silent_since = silent_since or time.time()
            if time.time() - silent_since > 300:
                say(f'[{name}] running but silent to Steam queries for 5 minutes; treating it as empty')
                return True
        else:
            silent_since = None
            say(f'[{name}] {h} playing; waiting for the server to empty')
        if time.time() > deadline:
            return False
        time.sleep(30)


def files_arrived(a, files):
    """Every file of the payload present at its size (ExtractArchive runs in the background)."""
    by_dir = {}
    for path, size in files.items():
        by_dir.setdefault(posixpath.dirname(path), {})[posixpath.basename(path)] = size
    for d, want in by_dir.items():
        have = a.ls(d)
        for fname, size in want.items():
            if fname not in have or int(have[fname].get('SizeBytes', -1)) != size:
                return False
    return True


def deploy(target, css_zip, mm_tar, tag, host, wait_empty, watch):
    iid, port, name = target
    a = Amp(iid)
    say(f'[{name}] AMP state {a.state()}')

    has_css = 'counterstrikesharp' in a.ls('game/csgo/addons')
    # Only expect CounterStrikeSharp to start where Metamod loads it (its .vdf can be switched off)
    css_enabled = has_css and 'counterstrikesharp.vdf' in a.ls(MM)
    moves = ([(CSS, p) for p in CSS_PARTS] if has_css else []) + ([(MM, 'bin')] if mm_tar else [])
    if not moves:
        say(f'[{name}] has neither CounterStrikeSharp nor a Metamod build to replace: nothing to do')
        return 'deployed'
    payload, files = payload_for(css_zip if has_css else None, mm_tar)

    if all(f'{part}.{tag}bak' in a.ls(folder) for folder, part in moves) and files_arrived(a, files):
        say(f'[{name}] already on {tag}')
        return 'deployed'
    for folder, part in moves:
        if f'{part}.{tag}bak' in a.ls(folder):
            raise RuntimeError(f'[{name}] {folder}/{part}.{tag}bak exists but the files are not this build: '
                               f'a previous deploy was interrupted. Check the server by hand.')

    if not wait_until_empty(a, host, port, name, wait_empty):
        say(f'[{name}] still busy after {wait_empty} min: skipped')
        return 'skipped'
    if not a.stop():
        raise RuntimeError(f'[{name}] did not stop; nothing changed')
    say(f'[{name}] stopped')

    moved = []

    def rollback(why):
        """Put back every folder moved aside, verify it, and start the server. Raises RollbackFailed if it can't."""
        say(f'[{name}] {why}; rolling back')
        problems = []
        if not a.stop():
            problems.append('it did not stop')
        for folder, part in moved:
            try:
                have = a.ls(folder)
                if part in have and f'{part}.{tag}bak' in have:
                    a.call('FileManagerPlugin/TrashDirectory', {'DirectoryName': f'{folder}/{part}'})
                if f'{part}.{tag}bak' in a.ls(folder):
                    a.rename_dir(f'{folder}/{part}.{tag}bak', part)
                have = a.ls(folder)
                if part not in have or f'{part}.{tag}bak' in have:
                    problems.append(f'{folder}/{part} is not back ({sorted(k for k in have if k.startswith(part))})')
            except Exception as e:  # keep going: put back as much as possible
                problems.append(f'{folder}/{part}: {e}')
        try:
            a.call('Core/Start')
        except Exception as e:
            problems.append(f'start: {e}')
        if problems:
            raise RollbackFailed(f'[{name}] ROLLBACK INCOMPLETE, the server needs a person: {"; ".join(problems)}')
        say(f'[{name}] old build back in place and starting')

    try:
        for folder, part in moves:
            if not a.rename_dir(f'{folder}/{part}', f'{part}.{tag}bak') or f'{part}.{tag}bak' not in a.ls(folder):
                raise RuntimeError(f'could not move {folder}/{part} aside')
            moved.append((folder, part))
        a.write('game/csgo/sharpupdater-deploy.zip', payload)
        a.call('FileManagerPlugin/ExtractArchive', {'ArchivePath': 'game/csgo/sharpupdater-deploy.zip', 'DestinationPath': 'game/csgo'})
        for _ in range(90):
            time.sleep(2)
            if files_arrived(a, files):
                break
        else:
            raise RuntimeError('the new files did not all arrive')
        a.call('FileManagerPlugin/TrashFile', {'Filename': 'game/csgo/sharpupdater-deploy.zip'})
        say(f'[{name}] {len(files)} new files in place')

        a.console()  # drop the old console backlog
        a.call('Core/Start')
        # AMP may first update the game (states 70/75) and takes a while to reach Running; the watch window starts
        # there. A segfault in the console, Failed, Stopped, or leaving Running after reaching it is a crash.
        started, running_since, css_up, answered, unknown = time.time(), None, False, False, 0
        while True:
            time.sleep(5)
            for line in a.console():
                if 'CounterStrikeSharp is starting up' in line:
                    css_up = True
                if re.search(r'segmentation fault|signal 11|core dumped|exited with code 139', line, re.I):
                    raise Crashed(line.strip()[:200])
            st = a.state()
            if st is None:  # one failed status call is not a crash
                unknown += 1
                if unknown >= 6:
                    raise RuntimeError('AMP stopped answering status calls')
                continue
            unknown = 0
            if st in (STOPPED, FAILED) and time.time() - started > 20:
                raise Crashed(f'AMP state {st}')
            if running_since is not None and st != RUNNING:
                raise Crashed(f'left Running (AMP state {st}): it crashed and AMP is restarting it')
            if st == RUNNING and running_since is None:
                running_since = time.time()
            if running_since is None:
                if time.time() - started > 30 * 60:
                    raise Crashed('never reached Running within 30 minutes')
                continue
            answered = answered or humans(host, port) is not None
            if time.time() - running_since > watch * 60:
                break
        if not answered:
            raise Crashed(f'not answering Steam queries {watch} min after starting')
        if css_enabled and not css_up:
            raise Crashed('CounterStrikeSharp never reported starting up')
    except Crashed as e:
        rollback(f'crashed on {tag}: {e}')
        return 'crashed'
    except BaseException as e:  # an AMP error, a timeout, the job being canceled: never leave it stopped
        rollback(f'deploy interrupted ({type(e).__name__}: {e})')
        raise
    say(f'[{name}] up and answering; CounterStrikeSharp '
        f'{"started" if css_up else "switched off (no counterstrikesharp.vdf)" if has_css else "not installed"}')
    return 'deployed'


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    signal.signal(signal.SIGTERM, _sigterm)
    css_zip, mm_tar = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None)
    targets = []
    for t in os.environ['AMP_TARGETS'].split(','):
        bits = t.strip().split(':')
        if len(bits) >= 2:
            targets.append((bits[0], int(bits[1]), bits[2] if len(bits) > 2 else bits[0][:8]))
    tag = re.sub(r'[^A-Za-z0-9]+', '-', os.environ.get('DEPLOY_TAG') or re.sub(r'^.*linux-|\.zip$', '', os.path.basename(css_zip)))[:40]
    deadline = T0 + float(os.environ.get('AMP_DEADLINE_MINUTES', 300)) * 60
    say(f'backups tagged {tag}; {len(targets)} server(s), {targets[0][2] if targets else "-"} first')

    results, code = {}, 0
    for i, target in enumerate(targets):
        # Leave room for a full wait, boot and watch, so the job is never killed mid-swap
        if time.time() + 45 * 60 > deadline:
            results[target[2]] = 'skipped (out of time)'
            code = max(code, 3)
            continue
        try:
            r = deploy(target, css_zip, mm_tar, tag, os.environ['AMP_GAME_HOST'],
                       float(os.environ.get('AMP_WAIT_EMPTY_MINUTES', 120)), float(os.environ.get('AMP_WATCH_MINUTES', 4)))
        except RollbackFailed as e:
            say(str(e))
            results[target[2]], code = 'ROLLBACK INCOMPLETE', 4
            break
        except Exception as e:
            say(f'[{target[2]}] error: {e}')
            results[target[2]], code = f'error ({e})'[:120], max(code, 3)
            break
        results[target[2]] = r
        if r == 'crashed':
            code = max(code, 2)
            break  # the rest keep their old build
        if r == 'skipped':
            code = max(code, 3)
            if i == 0:
                say('the canary was skipped, so nothing else is touched this run')
                break
    for target in targets:
        results.setdefault(target[2], 'not touched')
    say('done:', ', '.join(f'{k} {v}' for k, v in results.items()))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(f'### AMP deploy of {tag}\n' + ''.join(f'- {k}: {v}\n' for k, v in results.items()))
    sys.exit(code)


if __name__ == '__main__':
    main()
